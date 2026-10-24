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
$actions = @($rollbackPlan.managed_resources)
$rollbackReady = ($rollbackPlan.ready -and $manifest.state -ne 'rolled_back') -or ($manifest.state -eq 'rolling_back')
$planBlockers = @()
if ($manifest -and $manifest.state -eq 'rolled_back') { $planBlockers += 'already-rolled-back' }
elseif (-not $manifest) { $planBlockers += 'no-feature-manifest' }
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
if (-not $ConfirmManagementPath) { Write-Error 'SSH rollback requires -ConfirmManagementPath after local console or alternate access has been verified.'; exit 1 }
if (-not (Test-CurrentProcessAdministrator)) { Write-Error 'Run rollback from an elevated PowerShell session.'; exit 1 }
if (-not $PSCmdlet.ShouldProcess('feature-managed Windows SSH resources', 'Restore protected before-state')) { return }

try {
    $manifest.state = 'rolling_back'
    Save-RollbackManifest
    $resources = Get-RemoteAccessField $manifest 'resources'
    $serviceResource = Get-RemoteAccessField $resources 'ssh_service'
    $serviceBefore = Get-RemoteAccessField $serviceResource 'before'

    $service = Get-Service -Name sshd -ErrorAction SilentlyContinue
    if ($service -and [string]$service.Status -eq 'Running' -and ($serviceBefore.status -ne 'Running' -or -not $serviceBefore.existed)) {
        Stop-Service -Name sshd -ErrorAction Stop
    }

    foreach ($ruleName in @('AIStack-RemoteAccess-SSH-v4', 'AIStack-RemoteAccess-SSH-v6')) {
        $resourceName = if ($ruleName.EndsWith('-v4')) { 'ssh_firewall_v4' } else { 'ssh_firewall_v6' }
        if (Test-RemoteAccessResourceOwned -Manifest $manifest -Name $resourceName) {
            $resource = Get-RemoteAccessField $resources $resourceName
            $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
            if ($rule) {
                $actual = Get-RemoteAccessFirewallRuleSnapshot -Rule $rule
                if (-not (Test-RemoteAccessFirewallRuleSnapshot -Actual $actual -Expected $resource.expected)) { throw 'A feature-managed firewall rule has changed since setup; refusing to remove it.' }
                Remove-NetFirewallRule -Name $ruleName -ErrorAction Stop
            }
            $resource.managed = $false
            Save-RollbackManifest
        }
    }

    $defaultRuleResource = Get-RemoteAccessField $resources 'default_openssh_rule'
    if (Test-RemoteAccessResourceOwned -Manifest $manifest -Name 'default_openssh_rule') {
        $defaultRule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
        if ($defaultRuleResource.existed_before -eq $true) {
            if (-not $defaultRule) { throw 'A pre-existing default SSH firewall rule is missing; refusing to recreate it from an incomplete snapshot.' }
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
            if ([string]$defaultRule.Enabled -ne 'False' -or -not (Test-RemoteSshDefaultFirewallRule -Rule $shape -AllowDisabled)) { throw 'The default firewall rule changed after setup; refusing to remove it.' }
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
