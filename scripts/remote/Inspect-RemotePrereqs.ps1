#Requires -Version 5.1
[CmdletBinding()]
param([switch]$Json)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'RemoteAccess.Common.ps1')
$status = Get-RemoteAccessStatus

if ($Json) {
    $status | ConvertTo-Json -Depth 8
    return
}

Write-Output 'Remote-access prerequisite summary (identities and raw probe output are omitted):'
Write-Output ("Tailnet: {0}; unattended: {1}" -f $status.tailnet.state, $status.tailnet.unattended)
Write-Output ("SSH: {0}; firewall: {1}" -f $status.ssh.service, $status.ssh.firewall)
Write-Output ("Desktop task: {0}; session: {1}; rdc: {2}" -f $status.desktop.task, $status.desktop.session, $status.desktop.rdc)
Write-Output ("Human fallback: {0}" -f $status.human_fallback)
