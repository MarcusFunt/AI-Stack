#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$modulePath = Join-Path $repoRoot 'scripts\remote\RemoteAccess.Common.ps1'
if (-not (Test-Path -LiteralPath $modulePath)) {
    throw 'RemoteAccess.Common.ps1 is missing; remote status conversion is not implemented.'
}
. $modulePath

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)][AllowNull()][object]$Actual,
        [Parameter(Mandatory = $true)][AllowNull()][object]$Expected,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if ($Actual -ne $Expected) {
        throw "$Message; expected '$Expected', got '$Actual'"
    }
}

function Assert-NotContains {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Message
    )
    if ($Text.Contains($Value)) {
        throw $Message
    }
}

function New-InventoryFixture {
    return @{
        observed_at_utc = '2026-10-24T12:00:00Z'
        tailnet = @{
            state = 'connected'
            unattended = 'enabled'
            interface_present = $true
            address_present = $true
            last_state_change_utc = '2026-10-24T11:45:00Z'
            private_address = '100.64.12.34'
            identity = 'operator@example.invalid'
        }
        ssh = @{
            service = 'running'
            firewall = 'tailnet-only'
            last_listener_check_utc = '2026-10-24T11:59:00Z'
            raw_firewall_rule = 'private firewall rule name'
        }
        desktop = @{
            task = 'ready'
            session = 'interactive'
            rdc = 'ready'
            denied_by_category = @{
                authorization = 3
                view_only = 2
                other = 1
                'private identity' = 9
            }
            audit = @{
                valid = $true
                timestamp = '2026-10-24T11:58:00Z'
                category = 'view_only'
                raw_line = 'clipboard and screenshot contents'
            }
            screenshot = 'private screenshot payload'
            clipboard = 'private clipboard payload'
        }
        human_fallback = 'ready'
        host = @{
            uptime_seconds = 3600
            last_boot_utc = '2026-10-24T11:00:00Z'
        }
        raw_command_output = 'private command output'
    }
}

$status = ConvertTo-RemoteAccessStatus (New-InventoryFixture)
Assert-Equal $status.tailnet.state 'connected' 'A connected tailnet with interface and address evidence should be ready'
Assert-Equal $status.tailnet.unattended 'enabled' 'A verified unattended state should be preserved'
Assert-Equal $status.ssh.firewall 'tailnet-only' 'A scoped firewall should be reported as tailnet-only'
Assert-Equal $status.desktop.session 'interactive' 'An observed desktop session should be preserved'
Assert-Equal $status.desktop.denied_by_category.authorization 3 'Known denial counts should be preserved'
Assert-Equal $status.desktop.denied_by_category.view_only 2 'View-only denial counts should be preserved'
Assert-Equal $status.desktop.denied_by_category.other 1 'Other denial counts should be preserved'
Assert-Equal $status.last_audit.category 'view_only' 'The last audit category should be normalized'

$missingTailnet = New-InventoryFixture
$missingTailnet.tailnet.interface_present = $false
$missingTailnet.tailnet.address_present = $false
$missingTailnet.tailnet.state = 'connected'
$missingTailnet.tailnet.last_state_change_utc = $null
$missingStatus = ConvertTo-RemoteAccessStatus $missingTailnet
Assert-Equal $missingStatus.tailnet.state 'unknown' 'Missing Tailscale interface/address evidence must not imply connectivity'
Assert-Equal $missingStatus.tailnet.last_state_change_utc 'unknown' 'Missing state-change evidence must stay unknown'

$broadFirewall = New-InventoryFixture
$broadFirewall.ssh.firewall = 'broad'
$broadStatus = ConvertTo-RemoteAccessStatus $broadFirewall
Assert-Equal $broadStatus.ssh.firewall 'broad' 'A broad inbound allow must not be reported as tailnet-only'

$missingSsh = New-InventoryFixture
$missingSsh.ssh.service = 'missing'
$missingSshStatus = ConvertTo-RemoteAccessStatus $missingSsh
Assert-Equal $missingSshStatus.ssh.service 'missing' 'An absent OpenSSH service must be reported as missing'

$noDesktop = New-InventoryFixture
$noDesktop.desktop.session = 'not-observed'
$noDesktopStatus = ConvertTo-RemoteAccessStatus $noDesktop
Assert-Equal $noDesktopStatus.desktop.session 'unknown' 'An unrecognized session state must be unknown'

$badAudit = New-InventoryFixture
$badAudit.desktop.audit = @{ valid = $false; timestamp = 'raw audit'; category = 'operator@example.invalid' }
$badAudit.desktop.denied_by_category = $null
$badAuditStatus = ConvertTo-RemoteAccessStatus $badAudit
Assert-Equal $badAuditStatus.last_audit.timestamp 'unknown' 'Malformed audit timestamps must not be exposed'
Assert-Equal $badAuditStatus.last_audit.category 'unknown' 'Unrecognized audit categories must not be exposed'
Assert-Equal $badAuditStatus.desktop.denied_by_category.authorization $null 'Missing audit data must not be presented as zero denials'

$missingAudit = New-InventoryFixture
$missingAudit.desktop.audit = $null
$missingAudit.desktop.denied_by_category = $null
$missingAuditStatus = ConvertTo-RemoteAccessStatus $missingAudit
Assert-Equal $missingAuditStatus.last_audit.timestamp 'unknown' 'A missing audit log must report unknown'
Assert-Equal $missingAuditStatus.desktop.denied_by_category.authorization $null 'A missing audit log must not imply zero denials'

$unavailable = New-InventoryFixture
$unavailable.tailnet = @{ state = 'unknown'; unattended = 'unknown'; interface_present = $false; address_present = $false }
$unavailable.ssh = @{ service = 'unknown'; firewall = 'unknown' }
$unavailable.desktop = @{ task = 'unknown'; session = 'unknown'; rdc = 'not-checked' }
$unavailable.human_fallback = 'unknown'
$unavailable.host = @{}
$unavailableStatus = ConvertTo-RemoteAccessStatus $unavailable
Assert-Equal $unavailableStatus.tailnet.state 'unknown' 'Unavailable Tailscale probes must remain unknown'
Assert-Equal $unavailableStatus.ssh.service 'unknown' 'Unavailable SSH probes must remain unknown'
Assert-Equal $unavailableStatus.desktop.rdc 'not-checked' 'An unperformed rdc check must not claim readiness'
Assert-Equal $unavailableStatus.human_fallback 'unknown' 'Unavailable fallback probes must remain unknown'

$serialized = ConvertTo-Json -InputObject $status -Depth 8 -Compress
foreach ($privateValue in @(
    '100.64.12.34',
    'operator@example.invalid',
    'private firewall rule name',
    'clipboard and screenshot contents',
    'private screenshot payload',
    'private clipboard payload',
    'private command output',
    'private identity'
)) {
    Assert-NotContains $serialized $privateValue 'Remote-access status must omit raw identity, address, command, screen, clipboard, firewall, and audit data'
}

$expectedTopLevel = @('observed_at_utc', 'tailnet', 'ssh', 'desktop', 'human_fallback', 'host', 'last_audit')
$actualTopLevel = @($status.PSObject.Properties.Name | Sort-Object)
$expectedTopLevel = @($expectedTopLevel | Sort-Object)
Assert-Equal ($actualTopLevel -join ',') ($expectedTopLevel -join ',') 'The status contract must contain only enumerated top-level fields'

$unrelatedRule = @(@{
    local_port = @('Any')
    protocol = 'TCP'
    remote_addresses = @('Any')
    interface_aliases = @('Any')
    program = 'C:\OtherApp\service.exe'
})
$unrelatedFirewall = ConvertTo-RemoteAccessFirewallState -Rules $unrelatedRule -DefaultInboundAction 'Block' -ListenerProgramPaths @('C:\Windows\System32\wslrelay.exe')
Assert-Equal $unrelatedFirewall 'unknown' 'An unrelated application-wide allow must not be mistaken for an SSH port allow'

$broadPortRule = @(@{
    local_port = @('22')
    protocol = 'TCP'
    remote_addresses = @('Any')
    interface_aliases = @('Any')
    program = 'Any'
})
$broadPortFirewall = ConvertTo-RemoteAccessFirewallState -Rules $broadPortRule -DefaultInboundAction 'Block' -ListenerProgramPaths @()
Assert-Equal $broadPortFirewall 'broad' 'A port 22 allow from any remote address must be reported as broad'

$scopedPortRule = @(@{
    local_port = @('22')
    protocol = 'TCP'
    remote_addresses = @('100.64.0.0/10')
    interface_aliases = @('Tailscale')
    program = 'C:\Windows\System32\OpenSSH\sshd.exe'
})
$scopedFirewall = ConvertTo-RemoteAccessFirewallState -Rules $scopedPortRule -DefaultInboundAction 'Block' -ListenerProgramPaths @('C:\Windows\System32\OpenSSH\sshd.exe')
Assert-Equal $scopedFirewall 'tailnet-only' 'A port allow scoped to Tailscale addresses and interface must be reported tailnet-only'
Assert-Equal (ConvertTo-RemoteAccessFirewallState -Rules @() -DefaultInboundAction 'Allow' -ListenerProgramPaths @()) 'broad' 'An inbound-allow firewall default must be reported as broad'

Assert-Equal (Test-RemoteAccessTailnetAddress '100.64.10.2') $true 'Tailscale CGNAT addresses must be recognized'
Assert-Equal (Test-RemoteAccessTailnetAddress '100.1.10.2') $false 'Unrelated 100.0.0.0/8 addresses must not be treated as tailnet-only'
Assert-Equal (Test-RemoteAccessTailnetAddress 'fd7a:115c:a1e0::1') $true 'Tailscale IPv6 ULA addresses must be recognized'
Assert-Equal (Test-RemoteAccessTailnetAddress 'fd00::1') $false 'Unrelated IPv6 ULA addresses must not be treated as tailnet-only'

Assert-Equal (ConvertTo-RemoteAccessDesktopSessionState -InteractiveSessionPresent $true -InputDesktopName 'Default' -ProbeSucceeded $true) 'interactive' 'An unlocked interactive desktop should be reported as interactive'
Assert-Equal (ConvertTo-RemoteAccessDesktopSessionState -InteractiveSessionPresent $true -InputDesktopName 'Winlogon' -ProbeSucceeded $true) 'locked' 'A secure Winlogon desktop should be reported as locked'
Assert-Equal (ConvertTo-RemoteAccessDesktopSessionState -InteractiveSessionPresent $false -InputDesktopName $null -ProbeSucceeded $true) 'signed-out' 'No interactive session should be reported as signed out'
Assert-Equal (ConvertTo-RemoteAccessDesktopSessionState -InteractiveSessionPresent $true -InputDesktopName $null -ProbeSucceeded $false) 'unknown' 'An inaccessible input desktop must remain unknown'

Write-Output 'PASS: remote-access status fixtures are sanitized and fail closed.'
