#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [ValidateSet('Ssh')][string]$Component = 'Ssh',
    [switch]$Apply,
    [switch]$ConfirmManagementPath
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'RemoteAccess.Common.ps1')

function Test-CurrentProcessAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Save-RollbackManifest {
    $script:manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $script:manifestPath -Encoding UTF8
}

function Get-ValidatedRollbackManifest {
    param([string]$Path, [string]$StateRoot)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { $manifest = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -ErrorAction Stop } catch { throw 'invalid-manifest' }
    if ($manifest.schema_version -ne 1 -or $manifest.component -ne 'ssh' -or $manifest.state -notin @('applying', 'active', 'rolling_back', 'rolled_back')) { throw 'unsupported-manifest' }
    $root = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\') + '\'
    $snapshot = [IO.Path]::GetFullPath([string]$manifest.snapshot_path)
    if (-not $snapshot.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw 'snapshot-outside-root' }
    foreach ($protectedPath in @($Path, $snapshot)) {
        if (Test-Path -LiteralPath $protectedPath) {
            if ((Get-Item -LiteralPath $protectedPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse-protected-path' }
        }
    }
    $sid = [string]$manifest.account.sid
    if (-not (Test-RemoteAccessLocalAccountSid $sid)) { throw 'invalid-account-sid' }
    $localAccounts = @(Get-CimInstance -ClassName Win32_UserAccount -Filter "SID = '$sid'" -ErrorAction Stop)
    if ($localAccounts.Count -ne 1 -or -not (Test-RemoteAccessLocalAccountIdentity -Sid $sid -ExpectedName ([string]$manifest.account.name) -LocalAccount $localAccounts[0].LocalAccount -ActualName ([string]$localAccounts[0].Name))) { throw 'local-account-mismatch' }
    $profileKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\' + $sid
    $registeredProfile = [Environment]::ExpandEnvironmentVariables([string](Get-ItemProperty -LiteralPath $profileKey -Name ProfileImagePath -ErrorAction Stop).ProfileImagePath)
    if ([IO.Path]::GetFullPath([string]$manifest.account.profile_path) -ine [IO.Path]::GetFullPath($registeredProfile)) { throw 'profile-mismatch' }
    $expectedConfig = [IO.Path]::GetFullPath((Join-Path $env:ProgramData 'ssh\sshd_config'))
    $resources = Get-RemoteAccessField $manifest 'resources'
    $config = Get-RemoteAccessField $resources 'sshd_config'
    if ([IO.Path]::GetFullPath([string]$config.path) -ine $expectedConfig) { throw 'unexpected-config-path' }
    $keys = Get-RemoteAccessField $resources 'authorized_keys'
    $expectedKeys = Join-Path (Join-Path ([string]$manifest.account.profile_path) '.ssh') 'authorized_keys'
    if ([IO.Path]::GetFullPath([string]$keys.path) -ine [IO.Path]::GetFullPath($expectedKeys)) { throw 'unexpected-key-path' }
    foreach ($backup in @([string]$config.backup_path, [string]$config.package_default_backup, [string]$keys.backup_path)) {
        if ([string]::IsNullOrWhiteSpace($backup)) { continue }
        $fullBackup = [IO.Path]::GetFullPath($backup)
        if (-not $fullBackup.StartsWith($snapshot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'backup-outside-snapshot' }
        if (Test-Path -LiteralPath $fullBackup -PathType Leaf) {
            if ((Get-Item -LiteralPath $fullBackup -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse-backup' }
        }
    }
    foreach ($resourceName in @('ssh_firewall_v4', 'ssh_firewall_v6')) {
        $resource = Get-RemoteAccessField $resources $resourceName
        $expectedName = if ($resourceName -eq 'ssh_firewall_v4') { 'AIStack-RemoteAccess-SSH-v4' } else { 'AIStack-RemoteAccess-SSH-v6' }
        if ([string]$resource.name -ne $expectedName) { throw 'unexpected-firewall-name' }
    }
    return $manifest
}

function Restore-FileFromFeatureSnapshot {
    param([object]$Resource, [string]$Path, [string]$BackupPath, [string]$ExpectedHash)
    if (-not (Test-RemoteAccessResourceOwned -Manifest $script:manifest -Name 'sshd_config')) { return }
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        if ($ExpectedHash -and (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $ExpectedHash) {
            throw 'A feature-managed file has been edited since setup; refusing to overwrite local changes.'
        }
    }
    if ($Resource.existed -eq $true) {
        if (-not $BackupPath -or -not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) { throw 'A required protected before-state copy is missing.' }
        Copy-Item -LiteralPath $BackupPath -Destination $Path -Force
        if ($Resource.sddl) {
            $acl = New-Object Security.AccessControl.FileSecurity
            $acl.SetSecurityDescriptorSddlForm([string]$Resource.sddl)
            Set-Acl -LiteralPath $Path -AclObject $acl
        }
    } elseif (Test-Path -LiteralPath $Path -PathType Leaf) {
        Remove-Item -LiteralPath $Path -Force
    }
}

function Restore-AuthorizedKeys {
    param([object]$Resource)
    if (-not (Test-RemoteAccessResourceOwned -Manifest $script:manifest -Name 'authorized_keys')) { return }
    $path = [string]$Resource.path
    if ($Resource.sha256_after -and (Test-Path -LiteralPath $path -PathType Leaf)) {
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne [string]$Resource.sha256_after) {
            throw 'The feature-managed authorized_keys file has local edits; refusing to overwrite them.'
        }
    }
    if ($Resource.existed -eq $true) {
        if (-not $Resource.backup_path -or -not (Test-Path -LiteralPath ([string]$Resource.backup_path) -PathType Leaf)) { throw 'A required protected authorized_keys before-state copy is missing.' }
        Copy-Item -LiteralPath ([string]$Resource.backup_path) -Destination $path -Force
        if ($Resource.sddl) {
            $acl = New-Object Security.AccessControl.FileSecurity
            $acl.SetSecurityDescriptorSddlForm([string]$Resource.sddl)
            Set-Acl -LiteralPath $path -AclObject $acl
        }
    } elseif (Test-Path -LiteralPath $path -PathType Leaf) {
        Remove-Item -LiteralPath $path -Force
    }
    if ($Resource.directory_existed -ne $true -and (Test-Path -LiteralPath ([string]$Resource.directory_path) -PathType Container)) {
        $children = @(Get-ChildItem -LiteralPath ([string]$Resource.directory_path) -Force)
        if ($children.Count -eq 0) { Remove-Item -LiteralPath ([string]$Resource.directory_path) -Force }
        else { throw 'The feature-created .ssh directory contains additional files; refusing to remove them.' }
    }
}

function Get-SshDefaultRuleShape {
    param([Parameter(Mandatory = $true)][object]$Rule)
    $port = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $Rule -ErrorAction Stop
    $app = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $Rule -ErrorAction Stop
    $service = Get-NetFirewallServiceFilter -AssociatedNetFirewallRule $Rule -ErrorAction Stop
    $address = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $Rule -ErrorAction Stop
    return @{
        name = [string]$Rule.Name
        enabled = ([string]$Rule.Enabled -eq 'True')
        direction = [string]$Rule.Direction
        action = [string]$Rule.Action
        protocol = [string]$port.Protocol
        local_port = [string]$port.LocalPort
        remote_addresses = @($address.RemoteAddress | ForEach-Object { [string]$_ })
        program = [string]$app.Program
        service = [string]$service.Service
    }
}

function Get-RemoteSshRollbackPreflight {
    param([Parameter(Mandatory = $true)][object]$Manifest)

    $blockers = New-Object 'System.Collections.Generic.List[string]'
    $resources = Get-RemoteAccessField $Manifest 'resources'

    foreach ($item in @(
        @{ resource = 'ssh_firewall_v4'; name = 'AIStack-RemoteAccess-SSH-v4' },
        @{ resource = 'ssh_firewall_v6'; name = 'AIStack-RemoteAccess-SSH-v6' }
    )) {
        if (-not (Test-RemoteAccessResourceOwned -Manifest $Manifest -Name $item.resource)) { continue }
        try {
            $resource = Get-RemoteAccessField $resources $item.resource
            $rules = @(Get-RemoteAccessFirewallRulesByName -Name $item.name -PolicyStore PersistentStore)
            $activeRules = @(Get-RemoteAccessFirewallRulesByName -Name $item.name -PolicyStore ActiveStore)
            if ($rules.Count -gt 1) { throw 'duplicate-firewall-rule-name' }
            if ($rules.Count -eq 1 -or $activeRules.Count -gt 0) {
                if ($rules.Count -ne 1 -or $activeRules.Count -ne 1) { throw 'firewall-rule-not-uniquely-active' }
                if ($null -eq $resource.expected) { throw 'missing-expected-snapshot' }
                $actual = Get-RemoteAccessFirewallRuleSnapshot -Rule $rules[0]
                $activeActual = Get-RemoteAccessFirewallRuleSnapshot -Rule $activeRules[0]
                if (-not (Test-RemoteAccessFirewallRuleSnapshot -Actual $actual -Expected $resource.expected) -or -not (Test-RemoteAccessFirewallRuleSnapshot -Actual $activeActual -Expected $resource.expected)) { throw 'firewall-rule-changed' }
            }
        } catch { $blockers.Add("$($item.resource)-changed-or-unverifiable") }
    }

    $guardResource = Get-RemoteAccessField $resources 'ssh_install_guard'
    if ((Get-RemoteAccessField $guardResource 'managed') -eq $true) {
        try {
            $guardName = [string](Get-RemoteAccessField $guardResource 'name')
            if ($guardName -ne 'AIStack-RemoteAccess-SSH-InstallGuard-22') { throw 'unexpected-guard-name' }
            $guards = @(Get-RemoteAccessFirewallRulesByName -Name $guardName -PolicyStore PersistentStore)
            $activeGuards = @(Get-RemoteAccessFirewallRulesByName -Name $guardName -PolicyStore ActiveStore)
            if ($guards.Count -gt 0 -or $activeGuards.Count -gt 0) {
                if ($guards.Count -ne 1 -or $activeGuards.Count -ne 1) { throw 'guard-not-uniquely-active' }
                if ($null -eq (Get-RemoteAccessField $guardResource 'expected')) { throw 'missing-guard-snapshot' }
                $actual = Get-RemoteAccessFirewallRuleSnapshot -Rule $guards[0]
                $activeActual = Get-RemoteAccessFirewallRuleSnapshot -Rule $activeGuards[0]
                if (-not (Test-RemoteSshInstallGuardRule -Name $guardName -Rule $actual) -or -not (Test-RemoteAccessFirewallRuleSnapshot -Actual $actual -Expected $guardResource.expected) -or -not (Test-RemoteSshInstallGuardRule -Name $guardName -Rule $activeActual) -or -not (Test-RemoteAccessFirewallRuleSnapshot -Actual $activeActual -Expected $guardResource.expected)) { throw 'guard-changed' }
            } else {
                $port22State = Get-RemoteAccessFirewallState -Port 22 -ExpectedProgramPaths @((Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'))
                if ($port22State -ne 'blocked') { throw 'missing-guard-and-port22-not-proven-blocked' }
            }
        } catch { $blockers.Add('ssh-install-guard-changed-or-unverifiable') }
    } else {
        try {
            $unownedGuards = @(Get-RemoteAccessFirewallRulesByName -Name 'AIStack-RemoteAccess-SSH-InstallGuard-22' -PolicyStore PersistentStore)
            $activeUnownedGuards = @(Get-RemoteAccessFirewallRulesByName -Name 'AIStack-RemoteAccess-SSH-InstallGuard-22' -PolicyStore ActiveStore)
            if ($unownedGuards.Count -gt 0 -or $activeUnownedGuards.Count -gt 0) { $blockers.Add('unowned-ssh-install-guard-present') }
        } catch { $blockers.Add('ssh-install-guard-state-unverifiable') }
    }

    $defaultResource = Get-RemoteAccessField $resources 'default_openssh_rule'
    if ((Get-RemoteAccessField $defaultResource 'managed') -eq $true) {
        try {
            $defaultRules = @(Get-RemoteAccessFirewallRulesByName -Name 'OpenSSH-Server-In-TCP' -PolicyStore PersistentStore)
            $activeDefaultRules = @(Get-RemoteAccessFirewallRulesByName -Name 'OpenSSH-Server-In-TCP' -PolicyStore ActiveStore)
            if ($defaultRules.Count -gt 1) { throw 'duplicate-default-rule-name' }
            $defaultRule = if ($defaultRules.Count -eq 1) { $defaultRules[0] } else { $null }
            if ($defaultResource.existed_before -eq $true) {
                if (-not $defaultRule -or $activeDefaultRules.Count -ne 1) { throw 'preexisting-rule-missing-or-inactive' }
                $shape = Get-SshDefaultRuleShape -Rule $defaultRule
                $activeShape = Get-SshDefaultRuleShape -Rule $activeDefaultRules[0]
                if (-not (Test-RemoteSshDefaultFirewallRule -Rule $shape -AllowDisabled)) { throw 'preexisting-rule-changed' }
                if (-not (Test-RemoteSshDefaultFirewallRule -Rule $activeShape -AllowDisabled)) { throw 'preexisting-active-rule-changed' }
                if (-not (Test-RemoteAccessFirewallRuleSnapshot -Actual (Get-RemoteAccessFirewallRuleSnapshot -Rule $activeDefaultRules[0]) -Expected (Get-RemoteAccessFirewallRuleSnapshot -Rule $defaultRule))) { throw 'preexisting-rule-not-active-as-saved' }
                if ($defaultResource.enabled_before -notin @('True', 'False')) { throw 'preexisting-enabled-state-missing' }
            } elseif ($defaultRule) {
                if ($activeDefaultRules.Count -ne 1) { throw 'generated-rule-not-uniquely-active' }
                $shape = Get-SshDefaultRuleShape -Rule $defaultRule
                $activeShape = Get-SshDefaultRuleShape -Rule $activeDefaultRules[0]
                if ([string]$defaultRule.Enabled -ne 'False' -or -not (Test-RemoteSshDefaultFirewallRule -Rule $shape -AllowDisabled -AllowGeneratedUnbound)) { throw 'generated-rule-changed' }
                if ([string]$activeDefaultRules[0].Enabled -ne 'False' -or -not (Test-RemoteSshDefaultFirewallRule -Rule $activeShape -AllowDisabled -AllowGeneratedUnbound)) { throw 'generated-active-rule-changed' }
                if (-not (Test-RemoteAccessFirewallRuleSnapshot -Actual (Get-RemoteAccessFirewallRuleSnapshot -Rule $activeDefaultRules[0]) -Expected (Get-RemoteAccessFirewallRuleSnapshot -Rule $defaultRule))) { throw 'generated-rule-not-active-as-saved' }
            } elseif ($activeDefaultRules.Count -gt 0) {
                throw 'unowned-active-default-rule-present'
            }
        } catch { $blockers.Add('default-openssh-rule-changed-or-unverifiable') }
    }

    foreach ($resourceName in @('sshd_config', 'authorized_keys')) {
        if (-not (Test-RemoteAccessResourceOwned -Manifest $Manifest -Name $resourceName)) { continue }
        try {
            $resource = Get-RemoteAccessField $resources $resourceName
            $path = [string](Get-RemoteAccessField $resource 'path')
            if ([string]::IsNullOrWhiteSpace($path)) { throw 'missing-path' }
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse-current-file' }
                $currentHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash
                $expectedHash = if ($resourceName -eq 'sshd_config') { [string]$resource.sha256 } else { [string]$resource.sha256_after }
                if ($expectedHash) {
                    if ($currentHash -ne $expectedHash) { throw 'current-file-edited' }
                } elseif ($resource.existed -eq $true) {
                    $backupPath = [string]$resource.backup_path
                    if (-not $backupPath -or -not (Test-Path -LiteralPath $backupPath -PathType Leaf) -or $currentHash -ne (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256 -ErrorAction Stop).Hash) { throw 'before-state-file-changed' }
                } else { throw 'feature-file-has-no-recorded-hash' }
            } elseif (Test-Path -LiteralPath $path) { throw 'current-path-is-not-a-file' }

            if ($resource.existed -eq $true) {
                $backupPath = [string]$resource.backup_path
                if (-not $backupPath -or -not (Test-Path -LiteralPath $backupPath -PathType Leaf)) { throw 'required-backup-missing' }
                if ((Get-Item -LiteralPath $backupPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse-backup' }
            }
            if ($resourceName -eq 'authorized_keys') {
                $directoryPath = [string]$resource.directory_path
                if (Test-Path -LiteralPath $directoryPath -PathType Container) {
                    if ((Get-Item -LiteralPath $directoryPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse-key-directory' }
                    if ($resource.directory_existed -ne $true) {
                        $otherChildren = @(Get-ChildItem -LiteralPath $directoryPath -Force | Where-Object { $_.Name -ne 'authorized_keys' })
                        if ($otherChildren.Count -gt 0) { throw 'feature-directory-has-other-files' }
                    }
                } elseif (Test-Path -LiteralPath $directoryPath) { throw 'key-directory-path-is-not-a-directory' }
            }
        } catch { $blockers.Add("$resourceName-changed-or-unverifiable") }
    }

    $serviceResource = Get-RemoteAccessField $resources 'ssh_service'
    if ((Get-RemoteAccessField $serviceResource 'managed') -eq $true) {
        try {
            $before = Get-RemoteAccessField $serviceResource 'before'
            if ($null -eq $before -or $before.existed -notin @($true, $false)) { throw 'service-before-state-invalid' }
            $services = @(Get-Service -ErrorAction Stop | Where-Object { [string]$_.Name -ieq 'sshd' })
            if ($services.Count -gt 1) { throw 'duplicate-service-name' }
            $service = if ($services.Count -eq 1) { $services[0] } else { $null }
            if ($before.existed -eq $true) {
                if (-not $service -or $before.start_type -notin @('Auto', 'Manual', 'Disabled') -or $before.status -notin @('Running', 'Stopped')) { throw 'service-before-state-invalid' }
            }
        } catch { $blockers.Add('ssh-service-state-unverifiable') }
    }

    $capabilityResource = Get-RemoteAccessField $resources 'ssh_capability'
    if ((Test-RemoteAccessResourceOwned -Manifest $Manifest -Name 'ssh_capability') -and $capabilityResource.was_installed -ne $true) {
        try { [void](Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop) }
        catch { $blockers.Add('ssh-capability-state-unverifiable') }
    }

    return [pscustomobject]@{ ready = ($blockers.Count -eq 0); blockers = @($blockers.ToArray()) }
}

$stateRoot = Join-Path $env:ProgramData 'AI-Stack\RemoteAccess'
$manifestPath = Join-Path $stateRoot 'ssh-manifest.json'
$manifest = $null
try {
    if (Test-Path -LiteralPath $stateRoot -PathType Container) {
        if ((Get-Item -LiteralPath $stateRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse-state-directory' }
        $stateAcl = Get-Acl -LiteralPath $stateRoot
        if (-not $stateAcl.AreAccessRulesProtected) { throw 'unprotected-state-directory' }
        foreach ($rule in $stateAcl.Access) {
            if ($rule.AccessControlType -ne 'Allow') { continue }
            $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            if ($sid -notin @('S-1-5-18', 'S-1-5-32-544')) { throw 'untrusted-state-directory' }
        }
    }
    $manifest = Get-ValidatedRollbackManifest -Path $manifestPath -StateRoot $stateRoot
} catch {
    [pscustomobject]@{ component = $Component.ToLowerInvariant(); ready = $false; blockers = @('protected-manifest-invalid'); actions = @() } | ConvertTo-Json -Depth 4
    exit 1
}

$rollbackPlan = Get-RemoteAccessRollbackPlan -Manifest $manifest -Component $Component
$rollbackPreflight = if ($manifest) { Get-RemoteSshRollbackPreflight -Manifest $manifest } else { [pscustomobject]@{ ready = $false; blockers = @('no-feature-manifest') } }
$actions = @($rollbackPlan.managed_resources)
$rollbackReady = ($rollbackPlan.ready -and $rollbackPreflight.ready -and $manifest.state -ne 'rolled_back') -or ($manifest.state -eq 'rolling_back' -and $rollbackPreflight.ready)
$planBlockers = @()
if ($manifest -and $manifest.state -eq 'rolled_back') { $planBlockers += 'already-rolled-back' }
elseif (-not $manifest) { $planBlockers += 'no-feature-manifest' }
if ($manifest -and -not $rollbackPreflight.ready) { $planBlockers += @($rollbackPreflight.blockers) }
[pscustomobject]@{
    component = $Component.ToLowerInvariant()
    ready = $rollbackReady
    apply_requested = [bool]$Apply
    managed_resources = [string[]]$actions
    blockers = [string[]]$planBlockers
    state = if ($Apply) { 'preflight' } else { 'plan-only' }
} | ConvertTo-Json -Depth 4

if (-not $Apply) { return }
if (-not $manifest -or (-not $rollbackPlan.ready -and $manifest.state -ne 'rolling_back') -or $manifest.state -eq 'rolled_back') { Write-Error 'No active feature-managed SSH resources are available to roll back.'; exit 1 }
if (-not $rollbackPreflight.ready) { Write-Error 'SSH rollback preflight found a changed or unverifiable resource. No service or host state was changed.'; exit 1 }
if (-not $ConfirmManagementPath) { Write-Error 'SSH rollback requires -ConfirmManagementPath after local console or alternate access has been verified.'; exit 1 }
if (-not (Test-CurrentProcessAdministrator)) { Write-Error 'Run rollback from an elevated PowerShell session.'; exit 1 }
if (-not $PSCmdlet.ShouldProcess('feature-managed Windows SSH resources', 'Restore protected before-state')) { return }

$rollbackPreflight = Get-RemoteSshRollbackPreflight -Manifest $manifest
if (-not $rollbackPreflight.ready) { Write-Error 'SSH rollback preflight changed or became unverifiable before apply. No service or host state was changed.'; exit 1 }

try {
    $manifest.state = 'rolling_back'
    Save-RollbackManifest
    $resources = Get-RemoteAccessField $manifest 'resources'
    $serviceResource = Get-RemoteAccessField $resources 'ssh_service'
    $serviceBefore = Get-RemoteAccessField $serviceResource 'before'

    $services = @(Get-Service -ErrorAction Stop | Where-Object { [string]$_.Name -ieq 'sshd' })
    if ($services.Count -gt 1) { throw 'Multiple services use the sshd service name.' }
    $service = if ($services.Count -eq 1) { $services[0] } else { $null }
    if ($service -and [string]$service.Status -eq 'Running' -and ($serviceBefore.status -ne 'Running' -or -not $serviceBefore.existed)) {
        Stop-Service -Name sshd -ErrorAction Stop
    }

    foreach ($ruleName in @('AIStack-RemoteAccess-SSH-v4', 'AIStack-RemoteAccess-SSH-v6')) {
        $resourceName = if ($ruleName.EndsWith('-v4')) { 'ssh_firewall_v4' } else { 'ssh_firewall_v6' }
        if (Test-RemoteAccessResourceOwned -Manifest $manifest -Name $resourceName) {
            $resource = Get-RemoteAccessField $resources $resourceName
            $rules = @(Get-RemoteAccessFirewallRulesByName -Name $ruleName -PolicyStore PersistentStore)
            $activeRules = @(Get-RemoteAccessFirewallRulesByName -Name $ruleName -PolicyStore ActiveStore)
            if ($rules.Count -gt 1) { throw 'Multiple local firewall rules use a feature-owned SSH name.' }
            if ($rules.Count -eq 1 -or $activeRules.Count -gt 0) {
                if ($rules.Count -ne 1 -or $activeRules.Count -ne 1) { throw 'A feature-managed firewall rule is not uniquely active; refusing to remove it.' }
                $actual = Get-RemoteAccessFirewallRuleSnapshot -Rule $rules[0]
                $activeActual = Get-RemoteAccessFirewallRuleSnapshot -Rule $activeRules[0]
                if (-not (Test-RemoteAccessFirewallRuleSnapshot -Actual $actual -Expected $resource.expected) -or -not (Test-RemoteAccessFirewallRuleSnapshot -Actual $activeActual -Expected $resource.expected)) { throw 'A feature-managed firewall rule has changed since setup; refusing to remove it.' }
                Remove-NetFirewallRule -Name $ruleName -ErrorAction Stop
            }
            $resource.managed = $false
            Save-RollbackManifest
        }
    }

    $defaultRuleResource = Get-RemoteAccessField $resources 'default_openssh_rule'
    if (Test-RemoteAccessResourceOwned -Manifest $manifest -Name 'default_openssh_rule') {
        $defaultRules = @(Get-RemoteAccessFirewallRulesByName -Name 'OpenSSH-Server-In-TCP' -PolicyStore PersistentStore)
        if ($defaultRules.Count -gt 1) { throw 'Multiple local firewall rules use the OpenSSH default rule name.' }
        $defaultRule = if ($defaultRules.Count -eq 1) { $defaultRules[0] } else { $null }
        $activeDefaultRules = @(Get-RemoteAccessFirewallRulesByName -Name 'OpenSSH-Server-In-TCP' -PolicyStore ActiveStore)
        if ($defaultRuleResource.existed_before -eq $true) {
            if (-not $defaultRule -or $activeDefaultRules.Count -ne 1) { throw 'A pre-existing default SSH firewall rule is missing or inactive; refusing to recreate it from an incomplete snapshot.' }
            $portFilter = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $defaultRule -ErrorAction Stop
            $appFilter = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $defaultRule -ErrorAction Stop
            $serviceFilter = Get-NetFirewallServiceFilter -AssociatedNetFirewallRule $defaultRule -ErrorAction Stop
            $addressFilter = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $defaultRule -ErrorAction Stop
            $shape = @{
                name = $defaultRule.Name
                enabled = ([string]$defaultRule.Enabled -eq 'True')
                direction = [string]$defaultRule.Direction
                action = [string]$defaultRule.Action
                protocol = [string]$portFilter.Protocol
                local_port = [string]$portFilter.LocalPort
                remote_addresses = @($addressFilter.RemoteAddress | ForEach-Object { [string]$_ })
                program = [string]$appFilter.Program
                service = [string]$serviceFilter.Service
            }
            if (-not (Test-RemoteSshDefaultFirewallRule -Rule $shape -AllowDisabled)) { throw 'A pre-existing default SSH firewall rule changed after setup; refusing to modify it.' }
            if ($defaultRuleResource.enabled_before -in @('True', 'False')) {
                Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled ([string]$defaultRuleResource.enabled_before -eq 'True') -ErrorAction Stop | Out-Null
            } else { throw 'The saved default SSH firewall state is incomplete.' }
        } elseif ($defaultRule) {
            if ($activeDefaultRules.Count -ne 1) { throw 'The generated default SSH firewall rule is not uniquely active; refusing to remove it.' }
            $portFilter = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $defaultRule -ErrorAction Stop
            $appFilter = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $defaultRule -ErrorAction Stop
            $serviceFilter = Get-NetFirewallServiceFilter -AssociatedNetFirewallRule $defaultRule -ErrorAction Stop
            $addressFilter = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $defaultRule -ErrorAction Stop
            $shape = @{
                name = $defaultRule.Name
                enabled = ([string]$defaultRule.Enabled -eq 'True')
                direction = [string]$defaultRule.Direction
                action = [string]$defaultRule.Action
                protocol = [string]$portFilter.Protocol
                local_port = [string]$portFilter.LocalPort
                remote_addresses = @($addressFilter.RemoteAddress | ForEach-Object { [string]$_ })
                program = [string]$appFilter.Program
                service = [string]$serviceFilter.Service
            }
            if ([string]$defaultRule.Enabled -ne 'False' -or -not (Test-RemoteSshDefaultFirewallRule -Rule $shape -AllowDisabled -AllowGeneratedUnbound)) { throw 'The default firewall rule changed after setup; refusing to remove it.' }
            Remove-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction Stop
        }
        $defaultRuleResource.managed = $false
        Save-RollbackManifest
    }

    $configResource = Get-RemoteAccessField $resources 'sshd_config'
    if (Test-RemoteAccessResourceOwned -Manifest $manifest -Name 'sshd_config') {
        Restore-FileFromFeatureSnapshot -Resource $configResource -Path ([string]$configResource.path) -BackupPath ([string]$configResource.backup_path) -ExpectedHash ([string]$configResource.sha256)
        $configResource.managed = $false
        Save-RollbackManifest
    }
    $keysResource = Get-RemoteAccessField $resources 'authorized_keys'
    Restore-AuthorizedKeys -Resource $keysResource
    if (Test-RemoteAccessResourceOwned -Manifest $manifest -Name 'authorized_keys') {
        $keysResource.managed = $false
        Save-RollbackManifest
    }

    if ((Test-RemoteAccessResourceOwned -Manifest $manifest -Name 'ssh_service') -and $serviceBefore.existed -eq $true) {
        $startup = switch ([string]$serviceBefore.start_type) { 'Auto' { 'Automatic' } 'Manual' { 'Manual' } 'Disabled' { 'Disabled' } default { $null } }
        if (-not $startup) { throw 'The saved sshd startup mode cannot be restored safely.' }
        Set-Service -Name sshd -StartupType $startup -ErrorAction Stop
        if ($serviceBefore.status -eq 'Running') {
            Start-Service -Name sshd -ErrorAction Stop
        } elseif ($serviceBefore.status -eq 'Stopped') {
            Stop-Service -Name sshd -ErrorAction SilentlyContinue
        }
    }
    if (Test-RemoteAccessResourceOwned -Manifest $manifest -Name 'ssh_service') {
        $serviceResource.managed = $false
        Save-RollbackManifest
    }

    $capabilityResource = Get-RemoteAccessField $resources 'ssh_capability'
    if ((Test-RemoteAccessResourceOwned -Manifest $manifest -Name 'ssh_capability') -and $capabilityResource.was_installed -ne $true) {
        $capability = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop
        if ([string]$capability.State -eq 'Installed') { Remove-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop | Out-Null }
        $capabilityResource.managed = $false
        Save-RollbackManifest
    }

    $guardResource = Get-RemoteAccessField $resources 'ssh_install_guard'
    if (Test-RemoteAccessResourceOwned -Manifest $manifest -Name 'ssh_install_guard') {
        $port22Firewall = Get-RemoteAccessFirewallState -Port 22 -ExpectedProgramPaths @((Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'))
        if ($port22Firewall -ne 'blocked') { throw 'Inbound TCP 22 still has an allow rule or its scope is unknown; retaining the temporary guard for review.' }
        $guardName = [string]$guardResource.name
        $guards = @(Get-RemoteAccessFirewallRulesByName -Name $guardName -PolicyStore PersistentStore)
        $activeGuards = @(Get-RemoteAccessFirewallRulesByName -Name $guardName -PolicyStore ActiveStore)
        if ($guards.Count -gt 0 -or $activeGuards.Count -gt 0) {
            if ($guards.Count -ne 1 -or $activeGuards.Count -ne 1) { throw 'The temporary TCP 22 guard changed after rollback preflight; refusing to remove it.' }
            $guardSnapshot = Get-RemoteAccessFirewallRuleSnapshot -Rule $guards[0]
            $activeGuardSnapshot = Get-RemoteAccessFirewallRuleSnapshot -Rule $activeGuards[0]
            if (-not (Test-RemoteSshInstallGuardRule -Name $guardName -Rule $guardSnapshot) -or -not (Test-RemoteAccessFirewallRuleSnapshot -Actual $guardSnapshot -Expected $guardResource.expected) -or -not (Test-RemoteSshInstallGuardRule -Name $guardName -Rule $activeGuardSnapshot) -or -not (Test-RemoteAccessFirewallRuleSnapshot -Actual $activeGuardSnapshot -Expected $guardResource.expected)) { throw 'The temporary TCP 22 guard changed after rollback preflight; refusing to remove it.' }
            Remove-NetFirewallRule -Name $guardName -ErrorAction Stop
        }
        $guardResource.managed = $false
        Save-RollbackManifest
    }

    $manifest.state = 'rolled_back'
    Set-RemoteAccessManifestProperty -Manifest $manifest -Name 'rolled_back_at_utc' -Value ([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))
    Save-RollbackManifest
    [pscustomobject]@{ component = 'ssh'; state = 'rolled_back'; restored_resources = [string[]]$actions; snapshot_retained = $true } | ConvertTo-Json -Depth 5
} catch {
    $manifest.state = 'rolling_back'
    Save-RollbackManifest
    Write-Error 'Rollback stopped on a resource that did not match its protected feature-owned before-state. Keep local console or the proven alternate access path available and inspect the protected recovery record.'
    exit 1
}
