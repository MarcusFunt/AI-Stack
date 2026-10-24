#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [switch]$Apply,
    [switch]$ConfirmManagementPath
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'RemoteAccess.Common.ps1')

function Test-RdcRollbackAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Save-RdcRollbackManifest {
    $text = ConvertTo-Json -InputObject $script:manifest -Depth 10
    [IO.File]::WriteAllText($script:manifestPath, $text, (New-Object Text.UTF8Encoding($false)))
    $acl = Get-Acl -LiteralPath $script:manifestPath
    if (-not $acl.AreAccessRulesProtected) { throw 'The rdc manifest ACL is not protected.' }
}

function Get-RdcRollbackTaskFacts {
    param([string]$TaskName, [string]$UserSid, [string]$RdcPath, [string]$ExpectedTaskPath, [bool]$TaskManaged)
    if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) { return [pscustomobject]@{ present = $true; matches = $false; task = $null } }
    try {
        $tasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { [string]$_.TaskName -ieq $TaskName })
        if (-not $tasks) { return [pscustomobject]@{ present = $false; matches = $true; task = $null } }
        if (-not $TaskManaged) { return [pscustomobject]@{ present = $true; matches = $false; task = $null } }
        if ($tasks.Count -ne 1) { return [pscustomobject]@{ present = $true; matches = $false; task = $null } }
        $task = $tasks[0]
        $actions = @($task.Actions)
        $action = if ($actions.Count -eq 1) { $actions[0] } else { $null }
        $snapshot = @{
            name = [string]$task.TaskName
            expected_name = $TaskName
            path = [string]$task.TaskPath
            expected_task_path = $ExpectedTaskPath
            action_count = $actions.Count
            state = [string]$task.State
            user_sid = ConvertTo-RemoteAccessAccountSid ([string]$task.Principal.UserId)
            expected_user_sid = $UserSid
            run_level = [string]$task.Principal.RunLevel
            logon_type = [string]$task.Principal.LogonType
            executable = [string]$action.Execute
            expected_executable = $RdcPath
            arguments = [string]$action.Arguments
        }
        return [pscustomobject]@{ present = $true; matches = [bool](Test-RemoteAccessRdcTask -Task $snapshot); task = $task }
    } catch {
        return [pscustomobject]@{ present = $true; matches = $false; task = $null }
    }
}

function Get-RdcRollbackManifest {
    param([string]$Path, [string]$ProgramRoot, [string]$UserSid, [string]$ExpectedBinary, [string]$ExpectedConfig)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    foreach ($directory in @($ProgramRoot, (Join-Path $env:APPDATA 'rdc'))) {
        if (Test-Path -LiteralPath $directory -PathType Container) {
            if ((Get-Item -LiteralPath $directory -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'resource-parent-reparse-point' }
        }
    }
    if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'manifest-reparse-point' }
    $manifestAcl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $writeMask = [int][Security.AccessControl.FileSystemRights]::Write -bor [int][Security.AccessControl.FileSystemRights]::Modify -bor [int][Security.AccessControl.FileSystemRights]::Delete -bor [int][Security.AccessControl.FileSystemRights]::ChangePermissions -bor [int][Security.AccessControl.FileSystemRights]::TakeOwnership -bor [int][Security.AccessControl.FileSystemRights]::FullControl
    $manifestWriters = New-Object 'System.Collections.Generic.List[string]'
    foreach ($rule in $manifestAcl.Access) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or (([int]$rule.FileSystemRights -band $writeMask) -eq 0)) { continue }
        $manifestWriters.Add($rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value)
    }
    if (-not (Test-RemoteAccessRdcConfigAcl -IsProtected $manifestAcl.AreAccessRulesProtected -WriteSids $manifestWriters.ToArray() -OwnerSid (ConvertTo-RemoteAccessAccountSid ([string]$manifestAcl.Owner)) -AllowedSids @($UserSid, 'S-1-5-18', 'S-1-5-32-544'))) { throw 'untrusted-manifest-acl' }
    $manifest = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    if ($manifest.schema_version -ne 1 -or $manifest.component -ne 'rdc' -or $manifest.state -notin @('active', 'applying', 'rolling_back', 'rolled_back')) { throw 'unsupported-manifest' }
    if ([string]$manifest.account.sid -ne $UserSid) { throw 'account-mismatch' }
    if ([IO.Path]::GetFullPath([string]$manifest.resources.binary.path) -ine [IO.Path]::GetFullPath($ExpectedBinary)) { throw 'binary-path-mismatch' }
    if ([IO.Path]::GetFullPath([string]$manifest.resources.config.path) -ine [IO.Path]::GetFullPath($ExpectedConfig)) { throw 'config-path-mismatch' }
    $separator = [IO.Path]::DirectorySeparatorChar
    $root = [IO.Path]::GetFullPath($ProgramRoot).TrimEnd($separator) + $separator
    foreach ($pathValue in @($Path, $ExpectedBinary, [string]$manifest.resources.config.path)) {
        $fullPath = [IO.Path]::GetFullPath($pathValue)
        if ($pathValue -ieq [string]$manifest.resources.config.path) {
            $expectedRoot = [IO.Path]::GetFullPath((Join-Path $env:APPDATA 'rdc')).TrimEnd($separator) + $separator
            if (-not $fullPath.StartsWith($expectedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'config-outside-user-root' }
        } elseif (-not $fullPath.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw 'resource-outside-program-root' }
        if (Test-Path -LiteralPath $fullPath) {
            if ((Get-Item -LiteralPath $fullPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'resource-reparse-point' }
        }
    }
    if ([string]$manifest.resources.firewall.name -ne 'AIStack-RemoteAccess-Rdc') { throw 'unexpected-firewall-name' }
    if ([string]$manifest.task_name -ne ('dev.rdc.daemon.' + $env:USERNAME)) { throw 'unexpected-task-name' }
    if ($manifest.resources.task.managed -eq $true -and $manifest.state -notin @('applying', 'rolled_back') -and [string]::IsNullOrWhiteSpace([string]$manifest.resources.task.task_path)) { throw 'unexpected-task-path' }
    return $manifest
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$userSid = [string]$identity.User.Value
$programRoot = Join-Path $env:LOCALAPPDATA 'Programs\rdc'
$binaryPath = Join-Path $programRoot 'rdc.exe'
$manifestPath = Join-Path $programRoot 'ai-stack-manifest.json'
$configPath = Join-Path $env:APPDATA 'rdc\config.toml'
$manifest = $null
try {
    $manifest = Get-RdcRollbackManifest -Path $manifestPath -ProgramRoot $programRoot -UserSid $userSid -ExpectedBinary $binaryPath -ExpectedConfig $configPath
} catch {
    [pscustomobject]@{ component = 'rdc'; ready = $false; state = 'plan-only'; blockers = @('protected-manifest-invalid'); managed_resources = @() } | ConvertTo-Json -Depth 4
    exit 1
}
if (-not $manifest) {
    [pscustomobject]@{ component = 'rdc'; ready = $false; apply_requested = [bool]$Apply; state = 'plan-only'; managed_resources = @(); blockers = @('no-feature-manifest') } | ConvertTo-Json -Depth 4
    if ($Apply) { exit 1 }
    return
}

$taskName = if ($manifest) { [string]$manifest.task_name } else { 'dev.rdc.daemon.' + $env:USERNAME }
$taskFacts = Get-RdcRollbackTaskFacts -TaskName $taskName -UserSid $userSid -RdcPath $binaryPath -ExpectedTaskPath ([string]$manifest.resources.task.task_path) -TaskManaged ([bool]$manifest.resources.task.managed)
$firewallRuleName = 'AIStack-RemoteAccess-Rdc'
$firewallRule = Get-NetFirewallRule -Name $firewallRuleName -ErrorAction SilentlyContinue
$firewallMatches = -not $firewallRule
if ($firewallRule -and $manifest) {
    try {
        $actualRule = Get-RemoteAccessFirewallRuleSnapshot -Rule $firewallRule
        $firewallMatches = Test-RemoteAccessFirewallRuleSnapshot -Actual $actualRule -Expected $manifest.resources.firewall.expected
    } catch { $firewallMatches = $false }
}
if ($firewallRule -and -not $manifest) { $firewallMatches = $false }

$binaryHash = $null
$configHash = $null
if (Test-Path -LiteralPath $binaryPath -PathType Leaf) { $binaryHash = (Get-FileHash -LiteralPath $binaryPath -Algorithm SHA256).Hash }
if (Test-Path -LiteralPath $configPath -PathType Leaf) { $configHash = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash }
$rollbackPlan = Get-RemoteAccessRdcRollbackPlan -Manifest $manifest -CurrentBinaryHash $binaryHash -CurrentConfigHash $configHash -TaskMatchesManifest $taskFacts.matches -FirewallMatchesManifest $firewallMatches
$planBlockers = @($rollbackPlan.blockers)
if (-not $manifest) { $planBlockers += 'no-feature-manifest' }
if ($manifest -and $manifest.state -eq 'rolled_back') { $planBlockers += 'already-rolled-back' }
$ready = ($rollbackPlan.ready -and $manifest.state -ne 'rolled_back')
[pscustomobject]@{
    component = 'rdc'
    ready = $ready
    apply_requested = [bool]$Apply
    state = if ($Apply) { 'preflight' } else { 'plan-only' }
    managed_resources = [string[]]$rollbackPlan.managed_resources
    blockers = [string[]]$planBlockers
} | ConvertTo-Json -Depth 4

if (-not $Apply) { return }
if (-not $ready) { Write-Error 'No unchanged active rdc resources are available to roll back.'; exit 1 }
if (-not $ConfirmManagementPath) { Write-Error 'rdc rollback requires -ConfirmManagementPath after SSH or a local console has been verified.'; exit 1 }
if (-not (Test-RdcRollbackAdministrator)) { Write-Error 'Run rdc rollback from an elevated PowerShell session.'; exit 1 }
$managementStatus = Get-RemoteAccessStatus
if ($managementStatus.tailnet.state -ne 'connected' -or $managementStatus.tailnet.unattended -ne 'enabled' -or $managementStatus.ssh.service -ne 'running' -or $managementStatus.ssh.firewall -ne 'tailnet-only') {
    Write-Error 'The separate SSH management path is not currently proven ready.'
    exit 1
}
if (-not $PSCmdlet.ShouldProcess('feature-managed rdc desktop resources', 'Remove the rdc task, exact firewall rule, and pinned user-local files')) { return }

try {
    $script:manifest = $manifest
    $script:manifestPath = $manifestPath
    if ($manifest.resources.task.managed -eq $true) {
        if ($taskFacts.present -and -not $manifest.resources.task.task_path) {
            $manifest.resources.task.task_path = [string]$taskFacts.task.TaskPath
            Save-RdcRollbackManifest
        } elseif (-not $taskFacts.present) {
            $manifest.resources.task.managed = $false
            Save-RdcRollbackManifest
        }
    }
    $manifest.state = 'rolling_back'
    Save-RdcRollbackManifest

    if ($taskFacts.present) {
        if (-not $taskFacts.matches) { throw 'The rdc task changed after preflight; refusing to remove it.' }
        $processes = @(Get-Process -Name 'rdc' -ErrorAction SilentlyContinue | Where-Object { $_.Path -and ([IO.Path]::GetFullPath($_.Path) -ieq [IO.Path]::GetFullPath($binaryPath)) })
        if ($processes.Count -gt 0) {
            $uninstallOutput = @(& $binaryPath service uninstall 2>&1)
            if ($LASTEXITCODE -ne 0) { throw 'rdc service uninstall failed; inspect the signed-in user session.' }
        } else {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
        }
        $remainingTasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { [string]$_.TaskName -ieq $taskName })
        if ($remainingTasks.Count -gt 0) { throw 'The feature-owned rdc task remains registered after uninstall.' }
    }
    $manifest.resources.task.managed = $false
    Save-RdcRollbackManifest

    $rule = Get-NetFirewallRule -Name $firewallRuleName -ErrorAction SilentlyContinue
    if ($rule) {
        $actual = Get-RemoteAccessFirewallRuleSnapshot -Rule $rule
        if (-not (Test-RemoteAccessFirewallRuleSnapshot -Actual $actual -Expected $manifest.resources.firewall.expected)) { throw 'The rdc firewall rule changed after preflight; refusing to remove it.' }
        Remove-NetFirewallRule -Name $firewallRuleName -ErrorAction Stop
    }
    $manifest.resources.firewall.managed = $false
    Save-RdcRollbackManifest

    if (Test-Path -LiteralPath $configPath -PathType Leaf) {
        if ((Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash -ne [string]$manifest.resources.config.sha256) { throw 'The managed rdc config changed after preflight; refusing to remove it.' }
        Remove-Item -LiteralPath $configPath -Force -ErrorAction Stop
    }
    $manifest.resources.config.managed = $false
    Save-RdcRollbackManifest

    if (Test-Path -LiteralPath $binaryPath -PathType Leaf) {
        if ((Get-FileHash -LiteralPath $binaryPath -Algorithm SHA256).Hash -ne [string]$manifest.resources.binary.sha256) { throw 'The managed rdc binary changed after preflight; refusing to remove it.' }
        Remove-Item -LiteralPath $binaryPath -Force -ErrorAction Stop
    }
    $manifest.resources.binary.managed = $false
    $manifest.state = 'rolled_back'
    $manifest.rolled_back_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    Save-RdcRollbackManifest
    [pscustomobject]@{ component = 'rdc'; state = 'rolled_back'; restored_resources = [string[]]$rollbackPlan.managed_resources; manifest_retained = $true } | ConvertTo-Json -Depth 4
} catch {
    $manifest.state = 'rolling_back'
    Save-RdcRollbackManifest
    Write-Error 'rdc rollback stopped because a managed resource changed or could not be removed. Keep SSH or the local console available and inspect the user-local manifest.'
    exit 1
}
