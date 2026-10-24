#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Json,
    [switch]$RequireBootRecovery,
    [switch]$RequireDesktop
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'RemoteAccess.Common.ps1')
$status = Get-RemoteAccessStatus

$bootValues = @($status.tailnet.state, $status.tailnet.unattended, $status.ssh.service, $status.ssh.firewall)
if ($bootValues -contains 'disconnected' -or $bootValues -contains 'disabled' -or $bootValues -contains 'stopped' -or $bootValues -contains 'missing' -or $bootValues -contains 'broad') {
    $bootRecovery = 'not-ready'
} elseif ($status.tailnet.state -eq 'connected' -and $status.tailnet.unattended -eq 'enabled' -and $status.ssh.service -eq 'running' -and $status.ssh.firewall -eq 'tailnet-only') {
    $bootRecovery = 'ready'
} else {
    $bootRecovery = 'unknown'
}

$desktopValues = @($status.desktop.task, $status.desktop.session, $status.desktop.rdc)
if ($desktopValues -contains 'absent' -or $desktopValues -contains 'signed-out' -or $desktopValues -contains 'locked' -or $desktopValues -contains 'unavailable') {
    $desktopReadiness = 'not-ready'
} elseif ($status.desktop.task -in @('ready', 'running') -and $status.desktop.session -eq 'interactive' -and $status.desktop.rdc -eq 'ready') {
    $desktopReadiness = 'ready'
} else {
    $desktopReadiness = 'unknown'
}

if ($Json) {
    [pscustomobject]@{
        observed_at_utc = $status.observed_at_utc
        boot_recovery = $bootRecovery
        desktop_readiness = $desktopReadiness
        status = $status
    } | ConvertTo-Json -Depth 10
} else {
    Write-Output ("Boot recovery: {0}" -f $bootRecovery)
    Write-Output ("Desktop readiness: {0}" -f $desktopReadiness)
    Write-Output ("Tailnet: {0}; SSH: {1}; firewall: {2}" -f $status.tailnet.state, $status.ssh.service, $status.ssh.firewall)
    Write-Output ("Session: {0}; rdc: {1}; fallback: {2}" -f $status.desktop.session, $status.desktop.rdc, $status.human_fallback)
}

if ($RequireBootRecovery -and $bootRecovery -ne 'ready') { exit 1 }
if ($RequireDesktop -and $desktopReadiness -ne 'ready') { exit 1 }
