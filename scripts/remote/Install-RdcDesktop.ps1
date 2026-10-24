#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string[]]$AllowedIdentities,
    [string[]]$ViewOnlyIdentities = @(),
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ArchivePath,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$BindAddress,
    [ValidateRange(1, 65535)][int]$Port = 7770,
    [switch]$ConfirmSshRecoveryAccepted,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'RemoteAccess.Common.ps1')

$script:RdcVersion = '0.4.0'
$script:RdcReleaseCommit = 'e66001f'
$script:RdcArchiveName = 'rdc-windows-x86_64.zip'
$script:RdcArchiveSha256 = '521dbbb18d93b619e1e70692bf28fd8ff0ebf6d2ec18334b005806e546415fd0'
$script:RdcFirewallRuleName = 'AIStack-RemoteAccess-Rdc'

function Test-RdcCurrentAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-RdcAccountSid {
    param([AllowNull()][string]$Name)
    if ([string]::IsNullOrWhiteSpace($Name)) { return $null }
    try {
        if ($Name -match '^S-1-') { return ([Security.Principal.SecurityIdentifier]$Name).Value }
        return ([Security.Principal.NTAccount]$Name).Translate([Security.Principal.SecurityIdentifier]).Value
    } catch { return $null }
}

function Get-RdcAllowedWriterSids {
    param([Parameter(Mandatory = $true)][string]$Path)
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $writeMask = [int][Security.AccessControl.FileSystemRights]::Write -bor
        [int][Security.AccessControl.FileSystemRights]::Modify -bor
        [int][Security.AccessControl.FileSystemRights]::Delete -bor
        [int][Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [int][Security.AccessControl.FileSystemRights]::TakeOwnership -bor
        [int][Security.AccessControl.FileSystemRights]::FullControl
    $writeSids = New-Object 'System.Collections.Generic.List[string]'
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        if (([int]$rule.FileSystemRights -band $writeMask) -eq 0) { continue }
        try { $writeSids.Add($rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value) } catch { return $null }
    }
    return [pscustomobject]@{
        is_protected = [bool]$acl.AreAccessRulesProtected
        owner_sid = ConvertTo-RemoteAccessAccountSid ([string]$acl.Owner)
        write_sids = @($writeSids.ToArray())
    }
}

function Set-RdcProtectedAcl {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$OwnerSid,
        [switch]$Directory
    )

    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleAll($rule) }
    $owner = New-Object Security.Principal.SecurityIdentifier($OwnerSid)
    $acl.SetOwner($owner)
    foreach ($sidText in @($OwnerSid, 'S-1-5-18', 'S-1-5-32-544')) {
        $sid = New-Object Security.Principal.SecurityIdentifier($sidText)
        if ($Directory) {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, [Security.AccessControl.FileSystemRights]::FullControl, 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        } else {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, [Security.AccessControl.FileSystemRights]::FullControl, 'Allow')
        }
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
}

function Get-RdcTailnetBinding {
    param([Parameter(Mandatory = $true)][string]$Address)
    if (-not (Test-RemoteAccessTailnetAddress -Address $Address) -or $Address.Contains('/')) { return $null }
    $parsed = [Net.IPAddress]::Parse($Address)
    foreach ($adapter in @(Get-NetAdapter -ErrorAction Stop | Where-Object {
        $_.Status -eq 'Up' -and (($_.Name -match '(?i)tailscale') -or ($_.InterfaceDescription -match '(?i)tailscale'))
    })) {
        $addresses = @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -ErrorAction Stop | ForEach-Object { [string]$_.IPAddress })
        foreach ($candidate in $addresses) {
            if ((ConvertTo-RemoteAccessAddressKey $candidate) -eq (ConvertTo-RemoteAccessAddressKey $Address)) {
                return [pscustomobject]@{ address = $parsed.ToString(); alias = [string]$adapter.Name; family = $parsed.AddressFamily.ToString() }
            }
        }
    }
    return $null
}

function Test-RdcUserPathsSafe {
    param([string[]]$Paths, [string]$ProfileRoot)
    try {
        $separator = [IO.Path]::DirectorySeparatorChar
        $root = [IO.Path]::GetFullPath($ProfileRoot).TrimEnd($separator)
        foreach ($path in $Paths) {
            $fullPath = [IO.Path]::GetFullPath($path)
            if (-not $fullPath.StartsWith(($root + $separator), [StringComparison]::OrdinalIgnoreCase)) { return $false }
            $current = $root
            $relative = $fullPath.Substring($root.Length).TrimStart($separator)
            foreach ($part in $relative.Split($separator)) {
                if ([string]::IsNullOrWhiteSpace($part)) { continue }
                $current = Join-Path $current $part
                if (Test-Path -LiteralPath $current) {
                    if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
                }
            }
        }
        return $true
    } catch { return $false }
}

function Get-RdcFirewallDefaultAllow {
    try {
        $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop | Where-Object { $_.Enabled })
        if (-not $profiles) { return $null }
        $actions = @($profiles | ForEach-Object { [string]$_.DefaultInboundAction.ToString() } | Sort-Object -Unique)
        if ($actions -contains 'Allow') { return $true }
        if (@($actions | Where-Object { $_ -ne 'Block' }).Count -eq 0) { return $false }
        return $null
    } catch { return $null }
}

function Get-RdcTaskSnapshot {
    param(
        [Parameter(Mandatory = $true)][object]$Task,
        [Parameter(Mandatory = $true)][string]$ExpectedUserSid,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutable,
        [Parameter(Mandatory = $true)][string]$ExpectedName,
        [AllowNull()][string]$ExpectedTaskPath
    )
    $actions = @($Task.Actions)
    $action = if ($actions.Count -eq 1) { $actions[0] } else { $null }
    $principal = $Task.Principal
    return @{
        name = [string]$Task.TaskName
        expected_name = $ExpectedName
        path = [string]$Task.TaskPath
        expected_task_path = $ExpectedTaskPath
        action_count = $actions.Count
        state = [string]$Task.State
        user_sid = Get-RdcAccountSid ([string]$principal.UserId)
        expected_user_sid = $ExpectedUserSid
        run_level = [string]$principal.RunLevel
        logon_type = [string]$principal.LogonType
        executable = [string]$action.Execute
        expected_executable = $ExpectedExecutable
        arguments = [string]$action.Arguments
    }
}

function Get-RdcTaskFacts {
    param(
        [Parameter(Mandatory = $true)][string]$UserSid,
        [Parameter(Mandatory = $true)][string]$RdcPath,
        [Parameter(Mandatory = $true)][string]$TaskName,
        [AllowNull()][string]$ExpectedTaskPath
    )
    if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{ present = $false; valid = $false; running = $false; conflict = $true; task = $null }
    }
    try {
        $tasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { [string]$_.TaskName -ieq $TaskName })
        if (-not $tasks) { return [pscustomobject]@{ present = $false; valid = $false; running = $false; conflict = $false; task = $null } }
        if ($tasks.Count -ne 1) { return [pscustomobject]@{ present = $true; valid = $false; running = $false; conflict = $true; task = $null } }
        $snapshot = Get-RdcTaskSnapshot -Task $tasks[0] -ExpectedUserSid $UserSid -ExpectedExecutable $RdcPath -ExpectedName $TaskName -ExpectedTaskPath $ExpectedTaskPath
        $valid = Test-RemoteAccessRdcTask -Task $snapshot
        return [pscustomobject]@{
            present = $true
            valid = [bool]$valid
            running = ($valid -and [string]$snapshot.state -eq 'Running')
            conflict = (-not $valid)
            task = $tasks[0]
        }
    } catch {
        return [pscustomobject]@{ present = $false; valid = $false; running = $false; conflict = $true; task = $null }
    }
}

function Get-RdcPortInUse {
    param([int]$SelectedPort, [string]$ExpectedExecutable, [bool]$FeatureOwnsInstall)
    try {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $SelectedPort -ErrorAction SilentlyContinue)
        if (-not $listeners) { return $false }
        if (-not $FeatureOwnsInstall) { return $true }
        foreach ($listener in $listeners) {
            try {
                $process = Get-Process -Id ([int]$listener.OwningProcess) -ErrorAction Stop
                if ($process.ProcessName -ine 'rdc' -or [IO.Path]::GetFullPath([string]$process.Path) -ine [IO.Path]::GetFullPath($ExpectedExecutable)) { return $true }
            } catch { return $true }
        }
        return $false
    } catch { return $null }
}

function Test-RdcExistingManifest {
    param(
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$UserSid,
        [Parameter(Mandatory = $true)][string]$RdcPath,
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][string]$DesiredConfigHash,
        [Parameter(Mandatory = $true)][string]$BindAddress,
        [Parameter(Mandatory = $true)][string]$InterfaceAlias,
        [Parameter(Mandatory = $true)][int]$Port
    )
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) { return [pscustomobject]@{ state = 'absent'; manifest = $null } }
    try {
        if ((Get-Item -LiteralPath $ManifestPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse' }
        $allowedSids = @($UserSid, 'S-1-5-18', 'S-1-5-32-544')
        foreach ($protectedPath in @((Split-Path -Parent $ManifestPath), $ManifestPath, $RdcPath)) {
            if (-not (Test-Path -LiteralPath $protectedPath)) { continue }
            if ((Get-Item -LiteralPath $protectedPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'reparse-resource' }
            $aclFacts = Get-RdcAllowedWriterSids -Path $protectedPath
            if (-not (Test-RemoteAccessRdcConfigAcl -IsProtected $aclFacts.is_protected -WriteSids $aclFacts.write_sids -OwnerSid $aclFacts.owner_sid -AllowedSids $allowedSids)) { throw 'untrusted-acl' }
        }
        $manifest = Get-Content -LiteralPath $ManifestPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($manifest.schema_version -ne 1 -or $manifest.component -ne 'rdc' -or $manifest.state -ne 'active') { throw 'schema' }
        if ([string]$manifest.account.sid -ne $UserSid) { throw 'account' }
        if ([string]::IsNullOrWhiteSpace([string]$manifest.resources.task.task_path)) { throw 'task-path' }
        if ([IO.Path]::GetFullPath([string]$manifest.resources.binary.path) -ine [IO.Path]::GetFullPath($RdcPath)) { throw 'binary-path' }
        if ([IO.Path]::GetFullPath([string]$manifest.resources.config.path) -ine [IO.Path]::GetFullPath($ConfigPath)) { throw 'config-path' }
        if ([int]$manifest.port -ne $Port -or [string]$manifest.bind_address -ine $BindAddress -or [string]$manifest.interface_alias -ine $InterfaceAlias) { throw 'listener-scope' }
        if (-not (Test-Path -LiteralPath $RdcPath -PathType Leaf) -or -not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw 'missing-file' }
        if ((Get-FileHash -LiteralPath $RdcPath -Algorithm SHA256).Hash -ne [string]$manifest.resources.binary.sha256) { throw 'binary-drift' }
        if ((Get-FileHash -LiteralPath $ConfigPath -Algorithm SHA256).Hash -ne [string]$manifest.resources.config.sha256) { throw 'config-drift' }
        if ((Get-FileHash -LiteralPath $ConfigPath -Algorithm SHA256).Hash -ne $DesiredConfigHash) { throw 'scope-change' }
        if ([string]$manifest.archive.sha256 -ne $script:RdcArchiveSha256) { throw 'pin-change' }
        return [pscustomobject]@{ state = 'active-identical'; manifest = $manifest }
    } catch {
        return [pscustomobject]@{ state = 'conflict'; manifest = $null }
    }
}

function Save-RdcManifest {
    param([Parameter(Mandatory = $true)][object]$Manifest, [Parameter(Mandatory = $true)][string]$Path, [string]$UserSid)
    $text = ConvertTo-Json -InputObject $Manifest -Depth 10
    [IO.File]::WriteAllText($Path, $text, (New-Object Text.UTF8Encoding($false)))
    if ($UserSid) { Set-RdcProtectedAcl -Path $Path -OwnerSid $UserSid }
}

function New-RdcExpectedFirewallSnapshot {
    param([string]$BindAddress, [string]$InterfaceAlias, [string]$RdcPath, [int]$SelectedPort)
    $address = [Net.IPAddress]::Parse($BindAddress)
    $remote = if ($address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) { @('100.64.0.0/10') } else { @('fd7a:115c:a1e0::/48') }
    return @{
        enabled = 'True'; direction = 'Inbound'; action = 'Allow'; profile = 'Any'; protocol = 'TCP'; local_port = [string]$SelectedPort
        remote_addresses = $remote; local_addresses = @($address.ToString()); interface_aliases = @($InterfaceAlias); program = $RdcPath
    }
}

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$userSid = [string]$identity.User.Value
$sessionId = 0
try { $sessionId = [int](Get-Process -Id $PID -ErrorAction Stop).SessionId } catch { }
$sessionState = Get-RemoteAccessDesktopSessionState
$isAdministrator = Test-RdcCurrentAdministrator
$tailscaleBinding = $null
try { $tailscaleBinding = Get-RdcTailnetBinding -Address $BindAddress } catch { }
$rdcPath = Join-Path (Join-Path (Join-Path $env:LOCALAPPDATA 'Programs') 'rdc') 'rdc.exe'
$configRoot = Join-Path $env:APPDATA 'rdc'
$configPath = Join-Path $configRoot 'config.toml'
$programRoot = Split-Path -Parent $rdcPath
$manifestPath = Join-Path $programRoot 'ai-stack-manifest.json'
$taskName = 'dev.rdc.daemon.' + $env:USERNAME
$sshManifestPath = Join-Path (Join-Path (Join-Path $env:ProgramData 'AI-Stack') 'RemoteAccess') 'ssh-manifest.json'
$profileRoot = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
$userPathsSafe = Test-RdcUserPathsSafe -Paths @($env:LOCALAPPDATA, $env:APPDATA, $programRoot, $configRoot) -ProfileRoot $profileRoot

$configText = $null
$configHash = $null
$identitiesValid = $true
try {
    $configText = New-RemoteAccessRdcConfig -Port $Port -BindAddress $BindAddress -AllowedIdentities $AllowedIdentities -ViewOnlyIdentities $ViewOnlyIdentities
    $configHash = [Convert]::ToBase64String([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($configText)))
} catch {
    $identitiesValid = $false
}
$configHashBytes = $null
if ($configText) { $configHashBytes = [Convert]::ToBase64String([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($configText))) }
$configHashHex = if ($configHashBytes) { [BitConverter]::ToString([Convert]::FromBase64String($configHashBytes)).Replace('-', '') } else { '' }

$archiveHashValid = $false
$actualArchiveHash = $null
try {
    $archiveResolved = (Resolve-Path -LiteralPath $ArchivePath -ErrorAction Stop).Path
    if ((Get-Item -LiteralPath $archiveResolved -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'untrusted-archive-path' }
    $actualArchiveHash = (Get-FileHash -LiteralPath $archiveResolved -Algorithm SHA256 -ErrorAction Stop).Hash
    $archiveHashValid = Test-RemoteAccessRdcArchiveHash -ActualHash $actualArchiveHash -ExpectedHash $script:RdcArchiveSha256
} catch { $archiveResolved = $null }

$manifestCheck = [pscustomobject]@{ state = 'absent'; manifest = $null }
if ($configHashHex) {
    if ($tailscaleBinding) {
        $manifestCheck = Test-RdcExistingManifest -ManifestPath $manifestPath -UserSid $userSid -RdcPath $rdcPath -ConfigPath $configPath -DesiredConfigHash $configHashHex -BindAddress $tailscaleBinding.address -InterfaceAlias $tailscaleBinding.alias -Port $Port
    }
}
$expectedTaskPath = if ($manifestCheck.manifest) { [string]$manifestCheck.manifest.resources.task.task_path } else { $null }
$taskFacts = Get-RdcTaskFacts -UserSid $userSid -RdcPath $rdcPath -TaskName $taskName -ExpectedTaskPath $expectedTaskPath
$firewallRule = Get-NetFirewallRule -Name $script:RdcFirewallRuleName -ErrorAction SilentlyContinue
$featureState = [string]$manifestCheck.state
if ($featureState -eq 'absent' -and ((Test-Path -LiteralPath $programRoot -PathType Container) -or (Test-Path -LiteralPath $rdcPath) -or (Test-Path -LiteralPath $configPath) -or $taskFacts.present -or $firewallRule)) { $featureState = 'conflict' }
if ($taskFacts.conflict) { $featureState = 'conflict' }

$firewallState = 'unknown'
try { $firewallState = Get-RemoteAccessFirewallState -Port $Port -ExpectedProgramPaths @($rdcPath) } catch { }
if ($firewallRule -and $manifestCheck.manifest) {
    try {
        $actualRuleSnapshot = Get-RemoteAccessFirewallRuleSnapshot -Rule $firewallRule
        if (-not (Test-RemoteAccessFirewallRuleSnapshot -Actual $actualRuleSnapshot -Expected $manifestCheck.manifest.resources.firewall.expected)) { $featureState = 'conflict' }
    } catch { $featureState = 'conflict' }
} elseif ($firewallRule -and -not $manifestCheck.manifest) {
    $featureState = 'conflict'
}

$allowedAclSids = @($userSid, 'S-1-5-18', 'S-1-5-32-544')
$configAclValid = $true
if (Test-Path -LiteralPath $configPath -PathType Leaf) {
    try {
        $configAcl = Get-RdcAllowedWriterSids -Path $configPath
        $configAclValid = Test-RemoteAccessRdcConfigAcl -IsProtected $configAcl.is_protected -WriteSids $configAcl.write_sids -OwnerSid $configAcl.owner_sid -AllowedSids $allowedAclSids
    } catch { $configAclValid = $false }
}

$sshManifestState = 'missing'
try {
    if (Test-Path -LiteralPath $sshManifestPath -PathType Leaf) {
        $sshManifest = Get-Content -LiteralPath $sshManifestPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($sshManifest.schema_version -eq 1 -and $sshManifest.component -eq 'ssh') { $sshManifestState = [string]$sshManifest.state }
    }
} catch { $sshManifestState = 'unknown' }
$remoteStatus = Get-RemoteAccessStatus
$bootRecoveryReady = ($remoteStatus.tailnet.state -eq 'connected' -and $remoteStatus.tailnet.unattended -eq 'enabled' -and $remoteStatus.ssh.service -eq 'running' -and $remoteStatus.ssh.firewall -eq 'tailnet-only')
$defaultAllow = Get-RdcFirewallDefaultAllow
$portInUse = Get-RdcPortInUse -SelectedPort $Port -ExpectedExecutable $rdcPath -FeatureOwnsInstall:($featureState -eq 'active-identical')

$preflight = @{
    identities_valid = $identitiesValid
    archive_hash_valid = $archiveHashValid
    config_acl_valid = $configAclValid
    user_paths_safe = $userPathsSafe
    session_state = $sessionState
    current_session_id = $sessionId
    bind_address_valid = ($null -ne $tailscaleBinding)
    port_available = ($portInUse -eq $false)
    ssh_manifest_active = $sshManifestState
    ssh_acceptance_attested = [bool]$ConfirmSshRecoveryAccepted
    boot_recovery_ready = [bool]$bootRecoveryReady
    firewall_state = $firewallState
    firewall_default_allow = $defaultAllow
    firewall_admin_context = $isAdministrator
    firewall_rule_present = [bool]$firewallRule
    install_state = $featureState
    task_valid = [bool]$taskFacts.valid
    task_running = [bool]$taskFacts.running
}
$plan = Get-RemoteAccessRdcInstallPlan -Preflight $preflight
$publicPlan = [pscustomobject]@{
    state = if ($Apply) { 'apply-requested' } else { 'plan-only' }
    ready = $plan.ready
    version = $script:RdcVersion
    source_commit = $script:RdcReleaseCommit
    archive_sha256 = $script:RdcArchiveSha256
    identity_count = @($AllowedIdentities).Count + @($ViewOnlyIdentities).Count
    view_only_identity_count = @($ViewOnlyIdentities).Count
    port = $Port
    actions = @($plan.actions)
    blockers = @($plan.blockers)
    confirmation_required = -not [bool]$ConfirmSshRecoveryAccepted
}

if (-not $Apply -or -not $plan.ready) {
    $publicPlan | ConvertTo-Json -Depth 5
    if ($Apply -and -not $plan.ready) { exit 1 }
    return
}

if ($plan.actions.Count -eq 1 -and $plan.actions[0] -eq 'keep-healthy-managed-installation') {
    $publicPlan.state = 'unchanged'
    $publicPlan | ConvertTo-Json -Depth 5
    return
}

if (-not $PSCmdlet.ShouldProcess('signed-in desktop session', 'Install pinned rdc, add the Tailscale-only firewall rule, and register the standard-user logon task')) {
    $publicPlan.state = 'what-if'
    $publicPlan | ConvertTo-Json -Depth 5
    return
}

Invoke-RemoteAccessRdcMutation -Apply -Mutation {
    $currentSessionId = [int](Get-Process -Id $PID -ErrorAction Stop).SessionId
    if ($currentSessionId -le 0 -or (Get-RemoteAccessDesktopSessionState) -ne 'interactive') { throw 'Interactive desktop state changed; no rdc task was registered.' }
    if (-not (Test-RdcUserPathsSafe -Paths @($env:LOCALAPPDATA, $env:APPDATA, $programRoot, $configRoot) -ProfileRoot $profileRoot)) { throw 'The signed-in user profile path changed or is untrusted.' }
    if (-not $firewallRule -and -not (Test-RdcCurrentAdministrator)) { throw 'Administrator access is required for the exact Tailscale firewall rule.' }
    $latestStatus = Get-RemoteAccessStatus
    if (-not $ConfirmSshRecoveryAccepted -or $latestStatus.tailnet.state -ne 'connected' -or $latestStatus.tailnet.unattended -ne 'enabled' -or $latestStatus.ssh.service -ne 'running' -or $latestStatus.ssh.firewall -ne 'tailnet-only') { throw 'The accepted SSH recovery path is no longer proven ready.' }
    $latestSshManifest = $null
    try { $latestSshManifest = Get-Content -LiteralPath $sshManifestPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { }
    if (-not $latestSshManifest -or $latestSshManifest.schema_version -ne 1 -or $latestSshManifest.component -ne 'ssh' -or $latestSshManifest.state -ne 'active') { throw 'The active SSH recovery manifest changed after review.' }
    $expectedTaskPath = if ($manifestCheck.manifest) { [string]$manifestCheck.manifest.resources.task.task_path } else { $null }
    $liveTaskFacts = Get-RdcTaskFacts -UserSid $userSid -RdcPath $rdcPath -TaskName $taskName -ExpectedTaskPath $expectedTaskPath
    if (-not (Test-RemoteAccessRdcInstallTaskState -InstallState $featureState -TaskPresent ([bool]$liveTaskFacts.present) -TaskValid ([bool]$liveTaskFacts.valid))) { throw 'The rdc task appeared or changed after review; no existing task was overwritten.' }
    $taskFacts = $liveTaskFacts
    $latestBinding = Get-RdcTailnetBinding -Address $BindAddress
    if (-not $latestBinding -or $latestBinding.address -ine $tailscaleBinding.address -or $latestBinding.alias -ine $tailscaleBinding.alias) { throw 'The Tailscale listener address or interface changed after review.' }
    if ((Get-RdcFirewallDefaultAllow) -ne $false) { throw 'The inbound firewall policy is no longer confirmed as block by default.' }
    if ((Get-RemoteAccessFirewallState -Port $Port -ExpectedProgramPaths @($rdcPath)) -eq 'broad') { throw 'A broad firewall rule appeared after review.' }
    $liveFeatureRule = Get-NetFirewallRule -Name $script:RdcFirewallRuleName -ErrorAction SilentlyContinue
    if ([bool]$liveFeatureRule -ne [bool]$firewallRule) { throw 'The feature firewall rule changed after review.' }
    if ($liveFeatureRule -and $manifestCheck.manifest) {
        $liveRuleSnapshot = Get-RemoteAccessFirewallRuleSnapshot -Rule $liveFeatureRule
        if (-not (Test-RemoteAccessFirewallRuleSnapshot -Actual $liveRuleSnapshot -Expected $manifestCheck.manifest.resources.firewall.expected)) { throw 'The feature firewall scope changed after review.' }
    }
    $latestPortInUse = Get-RdcPortInUse -SelectedPort $Port -ExpectedExecutable $rdcPath -FeatureOwnsInstall:($featureState -eq 'active-identical')
    if ($latestPortInUse -ne $false) { throw 'The rdc port became occupied or its listener could not be verified.' }
    if (-not (Test-Path -LiteralPath $archiveResolved -PathType Leaf) -or -not (Test-RemoteAccessRdcArchiveHash -ActualHash (Get-FileHash -LiteralPath $archiveResolved -Algorithm SHA256).Hash -ExpectedHash $script:RdcArchiveSha256)) { throw 'The pinned rdc archive changed after review; no installation was made.' }

    $tempRoot = Join-Path $env:TEMP ('AIStackRdc-' + [Guid]::NewGuid().ToString('N'))
    $createdTempRoot = $false
    try {
        $expectedFirewall = New-RdcExpectedFirewallSnapshot -BindAddress $tailscaleBinding.address -InterfaceAlias $tailscaleBinding.alias -RdcPath $rdcPath -SelectedPort $Port
        if ($featureState -eq 'absent') {
            if ((Test-Path -LiteralPath $programRoot) -or (Test-Path -LiteralPath $rdcPath) -or (Test-Path -LiteralPath $manifestPath) -or (Test-Path -LiteralPath $configPath)) {
                throw 'An unmanaged rdc file appeared after review; refusing to overwrite it.'
            }
            New-Item -ItemType Directory -Path $tempRoot -ErrorAction Stop | Out-Null
            $createdTempRoot = $true
            Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
            $zip = [IO.Compression.ZipFile]::OpenRead($archiveResolved)
            try {
                $rdcEntries = @($zip.Entries | Where-Object { $_.FullName -ceq 'rdc.exe' })
                if ($rdcEntries.Count -ne 1 -or $rdcEntries[0].Length -le 0 -or $rdcEntries[0].Length -gt 104857600) { throw 'The verified archive does not contain one bounded root rdc.exe.' }
                $binaryEntry = $rdcEntries[0]
                $outputStream = New-Object IO.FileStream((Join-Path $tempRoot 'rdc.exe'), [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                try {
                    $inputStream = $binaryEntry.Open()
                    try { $inputStream.CopyTo($outputStream) } finally { $inputStream.Dispose() }
                } finally { $outputStream.Dispose() }
            } finally { $zip.Dispose() }
            $binaryHash = (Get-FileHash -LiteralPath (Join-Path $tempRoot 'rdc.exe') -Algorithm SHA256).Hash
            New-Item -ItemType Directory -Path $programRoot -ErrorAction Stop | Out-Null
            Set-RdcProtectedAcl -Path $programRoot -OwnerSid $userSid -Directory
            $manifest = [pscustomobject]@{
                schema_version = 1; component = 'rdc'; state = 'applying'; created_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
                account = @{ sid = $userSid; user_name = [string]$identity.Name; profile_path = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile) }
                archive = @{ version = $script:RdcVersion; source_commit = $script:RdcReleaseCommit; name = $script:RdcArchiveName; sha256 = $script:RdcArchiveSha256 }
                port = $Port; bind_address = $tailscaleBinding.address; interface_alias = $tailscaleBinding.alias; task_name = $taskName
                resources = @{
                    binary = @{ managed = $true; path = $rdcPath; sha256 = $binaryHash }
                    config = @{ managed = $true; path = $configPath; sha256 = $configHashHex }
                    firewall = @{ managed = $true; existed_before = $false; name = $script:RdcFirewallRuleName; expected = $expectedFirewall }
                    task = @{ managed = $true; name = $taskName; task_path = $null; run_level = 'Limited'; logon_type = 'Interactive' }
                }
            }
            Save-RdcManifest -Manifest $manifest -Path $manifestPath -UserSid $userSid
            New-Item -ItemType Directory -Path $configRoot -Force -ErrorAction Stop | Out-Null
            Move-Item -LiteralPath (Join-Path $tempRoot 'rdc.exe') -Destination $rdcPath -ErrorAction Stop
            Set-RdcProtectedAcl -Path $rdcPath -OwnerSid $userSid
            $configBytes = (New-Object Text.UTF8Encoding($false)).GetBytes($configText)
            $configStream = New-Object IO.FileStream($configPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try { $configStream.Write($configBytes, 0, $configBytes.Length) } finally { $configStream.Dispose() }
            Set-RdcProtectedAcl -Path $configPath -OwnerSid $userSid
        } else {
            $manifest = $manifestCheck.manifest
            $manifest.state = 'applying'
            Save-RdcManifest -Manifest $manifest -Path $manifestPath -UserSid $userSid
        }

        $binaryHash = (Get-FileHash -LiteralPath $rdcPath -Algorithm SHA256).Hash
        $installedConfigHash = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash
        if ($installedConfigHash -ne $configHashHex) { throw 'The local rdc config differs from the reviewed identity scope.' }
        if ($binaryHash -ne [string]$manifest.resources.binary.sha256 -or $installedConfigHash -ne [string]$manifest.resources.config.sha256) { throw 'A managed rdc file differs from its applying manifest.' }

        $rule = Get-NetFirewallRule -Name $script:RdcFirewallRuleName -ErrorAction SilentlyContinue
        if (-not $rule) {
            New-NetFirewallRule -Name $script:RdcFirewallRuleName -DisplayName 'AI-Stack rdc (Tailscale only)' -Direction Inbound -Action Allow -Enabled True -Profile Any -Protocol TCP -LocalPort $Port -LocalAddress @($tailscaleBinding.address) -RemoteAddress @($expectedFirewall.remote_addresses) -InterfaceAlias @($tailscaleBinding.alias) -Program $rdcPath -ErrorAction Stop | Out-Null
            $rule = Get-NetFirewallRule -Name $script:RdcFirewallRuleName -ErrorAction Stop
            $actualExpected = Get-RemoteAccessFirewallRuleSnapshot -Rule $rule
            if (-not (Test-RemoteAccessFirewallRuleSnapshot -Actual $actualExpected -Expected $expectedFirewall)) { throw 'The created rdc firewall rule does not match the exact tailnet scope.' }
            $manifest.resources.firewall.expected = $actualExpected
            Save-RdcManifest -Manifest $manifest -Path $manifestPath -UserSid $userSid
        } else {
            $actualExpected = Get-RemoteAccessFirewallRuleSnapshot -Rule $rule
            if (-not (Test-RemoteAccessFirewallRuleSnapshot -Actual $actualExpected -Expected $manifest.resources.firewall.expected)) { throw 'The feature-managed rdc firewall rule has local edits; refusing to change it.' }
        }
        $effectiveFirewall = Get-RemoteAccessFirewallState -Port $Port -ExpectedProgramPaths @($rdcPath)
        if ($effectiveFirewall -ne 'tailnet-only') { throw 'The effective rdc firewall policy is not proven tailnet-only; the daemon was not started.' }

        $expectedTaskPath = if ($manifest.resources.task.task_path) { [string]$manifest.resources.task.task_path } else { $null }
        $taskFacts = Get-RdcTaskFacts -UserSid $userSid -RdcPath $rdcPath -TaskName $taskName -ExpectedTaskPath $expectedTaskPath
        if (-not (Test-RemoteAccessRdcInstallTaskState -InstallState $featureState -TaskPresent ([bool]$taskFacts.present) -TaskValid ([bool]$taskFacts.valid))) { throw 'The rdc task appeared or changed before registration; refusing to replace it.' }
        if (-not $taskFacts.valid) {
            $installOutput = @(& $rdcPath service install 2>&1)
            if ($LASTEXITCODE -ne 0) { throw 'rdc service install failed; inspect the signed-in desktop locally.' }
        } elseif (-not $taskFacts.running) {
            Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
        }

        $deadline = [DateTime]::UtcNow.AddSeconds(15)
        $taskVerified = $false
        $listenerVerified = $false
        while ([DateTime]::UtcNow -lt $deadline) {
            $expectedTaskPath = if ($manifest.resources.task.task_path) { [string]$manifest.resources.task.task_path } else { $null }
            $taskFacts = Get-RdcTaskFacts -UserSid $userSid -RdcPath $rdcPath -TaskName $taskName -ExpectedTaskPath $expectedTaskPath
            $rdcProcesses = @(Get-Process -Name 'rdc' -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $currentSessionId -and $_.Path -and ([IO.Path]::GetFullPath($_.Path) -ieq [IO.Path]::GetFullPath($rdcPath)) })
            $processIds = @($rdcProcesses | ForEach-Object { [int]$_.Id })
            $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | ForEach-Object { @{ local_address = [string]$_.LocalAddress; owning_process = [int]$_.OwningProcess } })
            $taskVerified = ($taskFacts.valid -and $taskFacts.running)
            $listenerVerified = Test-RemoteAccessRdcListener -Listeners $listeners -RdcProcessIds $processIds -TailnetAddresses @($tailscaleBinding.address)
            if ($taskVerified -and $listenerVerified) { break }
            Start-Sleep -Seconds 1
        }
        if (-not $taskVerified -or -not $listenerVerified) { throw 'rdc task or tailnet-bound listener did not become ready in the interactive session.' }

        $manifest.resources.task.task_path = [string]$taskFacts.task.TaskPath
        $manifest.resources.binary.sha256 = (Get-FileHash -LiteralPath $rdcPath -Algorithm SHA256).Hash
        $manifest.resources.config.sha256 = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash
        $manifest.state = 'active'
        Save-RdcManifest -Manifest $manifest -Path $manifestPath -UserSid $userSid
        [pscustomobject]@{ state = 'applied'; ready = $true; version = $script:RdcVersion; port = $Port; firewall = 'tailnet-only'; task_integrity = 'standard'; manifest = 'user-local-protected-state' } | ConvertTo-Json -Depth 4
    } finally {
        if ($createdTempRoot -and (Test-Path -LiteralPath $tempRoot)) { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue }
    }
}
