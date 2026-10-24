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
Assert-Equal (ConvertTo-RemoteAccessFirewallState -Rules $broadPortRule -DefaultInboundAction 'Block' -ListenerProgramPaths @() -Port 2222) 'unknown' 'A rule for a different port must not be treated as SSH exposure'

$alternatePortRule = @(@{
    local_port = @('2222')
    protocol = 'TCP'
    remote_addresses = @('Any')
    interface_aliases = @('Any')
    program = 'Any'
})
Assert-Equal (ConvertTo-RemoteAccessFirewallState -Rules $alternatePortRule -DefaultInboundAction 'Block' -ListenerProgramPaths @() -Port 2222) 'broad' 'A selected alternate SSH port must be checked for broad rules'
$portRangeRule = @(@{
    local_port = @('2200-2300')
    protocol = 'TCP'
    remote_addresses = @('Any')
    interface_aliases = @('Any')
    program = 'Any'
})
Assert-Equal (ConvertTo-RemoteAccessFirewallState -Rules $portRangeRule -DefaultInboundAction 'Block' -ListenerProgramPaths @() -Port 2222) 'broad' 'A range that includes the selected SSH port must be detected'

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
Assert-Equal (Test-RemoteSshClientAddress '100.64.10.20') $true 'A verified exact tailnet client address should be accepted'
Assert-Equal (Test-RemoteSshClientAddress '100.64.0.0/10') $false 'A client subnet must not silently widen an SSH source allowlist'
$ed25519Type = [Text.Encoding]::ASCII.GetBytes('ssh-ed25519')
$ed25519Blob = New-Object byte[] (4 + $ed25519Type.Length + 32)
$ed25519Blob[3] = [byte]$ed25519Type.Length
[Array]::Copy($ed25519Type, 0, $ed25519Blob, 4, $ed25519Type.Length)
$fixturePublicKey = 'ssh-ed25519 ' + [Convert]::ToBase64String($ed25519Blob) + ' fixture-comment'
$parsedPublicKey = ConvertFrom-RemoteOpenSshPublicKey -KeyText $fixturePublicKey
Assert-Equal $parsedPublicKey.type 'ssh-ed25519' 'A valid OpenSSH key blob should be recognized'
Assert-Equal $parsedPublicKey.line.Contains('fixture-comment') $false 'The installer should omit public-key comments from its managed authorization line'
$mismatchedKeyRejected = $false
try { [void](ConvertFrom-RemoteOpenSshPublicKey -KeyText ('ssh-rsa ' + [Convert]::ToBase64String($ed25519Blob))) } catch { $mismatchedKeyRejected = $true }
Assert-Equal $mismatchedKeyRejected $true 'The declared public-key type must match the encoded blob type'
Assert-Equal (Get-RemoteAccessSshStatusPort -Manifest $null) 22 'An unconfigured SSH stack should use the standard status port'
Assert-Equal (Get-RemoteAccessSshStatusPort -Manifest @{ schema_version = 1; component = 'ssh'; state = 'active'; port = 2222 }) 2222 'An active feature manifest should make the selected SSH port available to readiness probes'
Assert-Equal (Get-RemoteAccessSshStatusPort -Manifest @{ schema_version = 1; component = 'ssh'; state = 'applying'; port = 2222 }) $null 'An incomplete SSH apply must report no trusted port'
$sshScopeManifest = @{
    schema_version = 1; component = 'ssh'; state = 'active'; port = 2222
    account = @{ sid = 'S-1-5-21-100-200-300-1001' }
    client_addresses = @('100.64.10.20'); listener_addresses = @('100.64.10.2')
    listener_interfaces = @('Tailscale')
}
Assert-Equal (Test-RemoteSshManifestScope -Manifest $sshScopeManifest -AccountSid 'S-1-5-21-100-200-300-1001' -Port 2222 -ClientAddresses @('100.64.10.20') -ListenerAddresses @('100.64.10.2/32') -InterfaceAliases @('Tailscale')) $true 'An unchanged SSH manifest scope should remain idempotent'
Assert-Equal (Test-RemoteSshManifestScope -Manifest $sshScopeManifest -AccountSid 'S-1-5-21-100-200-300-1001' -Port 2222 -ClientAddresses @('100.64.10.21') -ListenerAddresses @('100.64.10.2') -InterfaceAliases @('Tailscale')) $false 'Changing an approved SSH client address requires a reviewed rollback and reapply'
$validSshListener = @(@{ local_address = '100.64.10.2'; owning_process = 1234 })
Assert-Equal (Test-RemoteAccessSshListener -Listeners $validSshListener -SshdProcessIds @(1234) -TailscaleAddresses @('100.64.10.2')) $true 'SSH readiness requires sshd bound to a verified local tailnet address'
Assert-Equal (Test-RemoteAccessSshListener -Listeners @(@{ local_address = '0.0.0.0'; owning_process = 1234 }) -SshdProcessIds @(1234) -TailscaleAddresses @('100.64.10.2')) $false 'SSH readiness must reject a wildcard listener'
Assert-Equal (Test-RemoteAccessSshListener -Listeners $validSshListener -SshdProcessIds @(9999) -TailscaleAddresses @('100.64.10.2')) $false 'SSH readiness must reject a listener owned by another process'

Assert-Equal (ConvertTo-RemoteAccessDesktopSessionState -InteractiveSessionPresent $true -InputDesktopName 'Default' -ProbeSucceeded $true) 'interactive' 'An unlocked interactive desktop should be reported as interactive'
Assert-Equal (ConvertTo-RemoteAccessDesktopSessionState -InteractiveSessionPresent $true -InputDesktopName 'Winlogon' -ProbeSucceeded $true) 'locked' 'A secure Winlogon desktop should be reported as locked'
Assert-Equal (ConvertTo-RemoteAccessDesktopSessionState -InteractiveSessionPresent $false -InputDesktopName $null -ProbeSucceeded $true) 'signed-out' 'No interactive session should be reported as signed out'
Assert-Equal (ConvertTo-RemoteAccessDesktopSessionState -InteractiveSessionPresent $true -InputDesktopName $null -ProbeSucceeded $false) 'unknown' 'An inaccessible input desktop must remain unknown'

function New-SshPreflightFixture {
    return @{
        account_is_standard = $true
        public_key_valid = $true
        manifest_scope_matches = $true
        trusted_client_addresses = @('100.64.10.20')
        port = 22
        alternate_port_approved = $false
        port_in_use = $false
        custom_sshd_config = $false
        authorized_key_exists = $false
        service_state = 'missing'
        firewall_state = 'unknown'
        firewall_default_allow = $false
        default_openssh_rule_state = 'absent'
        finalize_firewall = $false
        second_device_verified = $false
    }
}

$missingAddresses = New-SshPreflightFixture
$missingAddresses.trusted_client_addresses = @()
$missingAddressPlan = Get-RemoteSshInstallPlan $missingAddresses
Assert-Equal $missingAddressPlan.ready $false 'SSH setup must reject an empty trusted-address list'
Assert-Equal ($missingAddressPlan.blockers -contains 'trusted-client-address-required') $true 'The empty-address blocker must be explicit'

$untrustedAddresses = New-SshPreflightFixture
$untrustedAddresses.trusted_client_addresses = @('192.168.1.20')
$untrustedAddressPlan = Get-RemoteSshInstallPlan $untrustedAddresses
Assert-Equal ($untrustedAddressPlan.blockers -contains 'trusted-client-address-invalid') $true 'Non-tailnet client addresses must be rejected'

$occupiedPort = New-SshPreflightFixture
$occupiedPort.port_in_use = $true
$occupiedPort.port_owner = 'wslrelay'
$occupiedPortPlan = Get-RemoteSshInstallPlan $occupiedPort
Assert-Equal ($occupiedPortPlan.blockers -contains 'selected-port-in-use') $true 'The installer must refuse a port already owned by WSL or another service'

$alternatePort = New-SshPreflightFixture
$alternatePort.port = 2222
$alternatePortPlan = Get-RemoteSshInstallPlan $alternatePort
Assert-Equal ($alternatePortPlan.blockers -contains 'alternate-port-approval-required') $true 'An alternate SSH port requires explicit operator approval'
$alternatePort.alternate_port_approved = $true
Assert-Equal (Get-RemoteSshInstallPlan $alternatePort).ready $true 'An explicitly approved free tailnet SSH port may be planned'

$existingOpenSshRule = New-SshPreflightFixture
$existingOpenSshRule.default_openssh_rule_state = 'preexisting-broad'
$existingOpenSshRule.firewall_state = 'broad'
$existingOpenSshRulePlan = Get-RemoteSshInstallPlan $existingOpenSshRule
Assert-Equal ($existingOpenSshRulePlan.blockers -contains 'preexisting-broad-openssh-rule') $true 'A broad Windows OpenSSH rule that predates this feature must not be changed automatically'

$unownedBroadRule = New-SshPreflightFixture
$unownedBroadRule.firewall_state = 'broad'
$unownedBroadPlan = Get-RemoteSshInstallPlan $unownedBroadRule
Assert-Equal ($unownedBroadPlan.blockers -contains 'unowned-broad-firewall-rule') $true 'Any pre-existing broad effective firewall policy must block SSH setup'
$inboundAllow = New-SshPreflightFixture
$inboundAllow.firewall_default_allow = $true
Assert-Equal ((Get-RemoteSshInstallPlan $inboundAllow).blockers -contains 'inbound-firewall-default-allows') $true 'An enabled firewall profile with inbound default allow must block setup'

$customConfig = New-SshPreflightFixture
$customConfig.custom_sshd_config = $true
$customConfigPlan = Get-RemoteSshInstallPlan $customConfig
Assert-Equal ($customConfigPlan.blockers -contains 'existing-sshd-config-conflict') $true 'Custom SSH configuration must not be overwritten'

$existingKey = New-SshPreflightFixture
$existingKey.authorized_key_exists = $true
$existingKeyPlan = Get-RemoteSshInstallPlan $existingKey
Assert-Equal ($existingKeyPlan.actions -contains 'keep-authorized-key') $true 'An existing authorized key must not be duplicated or replaced'

$healthyService = New-SshPreflightFixture
$healthyService.service_state = 'running'
$healthyServicePlan = Get-RemoteSshInstallPlan $healthyService
Assert-Equal ($healthyServicePlan.actions -contains 'keep-running-service') $true 'A healthy SSH service must remain running during repeated planning'
Assert-Equal ($healthyServicePlan.actions -contains 'restart-sshd') $false 'Planning must not propose an unnecessary service restart'

$broadFinalize = New-SshPreflightFixture
$broadFinalize.firewall_state = 'broad'
$broadFinalize.finalize_firewall = $true
$broadFinalize.default_openssh_rule_state = 'feature-managed-broad'
$broadFinalize.broad_rule_manifested = $true
$broadFinalizePlan = Get-RemoteSshInstallPlan $broadFinalize
Assert-Equal ($broadFinalizePlan.blockers -contains 'second-device-verification-required') $true 'A broad allow must not be finalized before a second-device key login'
Assert-Equal ($broadFinalizePlan.actions -contains 'disable-broad-default-rule') $false 'The broad rule must remain until second-device verification'
$broadFinalize.second_device_verified = $true
Assert-Equal ((Get-RemoteSshInstallPlan $broadFinalize).actions -contains 'disable-broad-default-rule') $true 'A manifested broad OpenSSH rule may be disabled after explicit second-device confirmation'

$managedConfig = New-RemoteSshManagedConfig -Port 2222 -WindowsAccount 'AIStackAgent' -ListenAddresses @('100.64.10.2', 'fd7a:115c:a1e0::2')
Assert-Equal $managedConfig.Contains('Port 2222') $true 'Managed sshd config must use the selected port'
Assert-Equal $managedConfig.Contains('AllowUsers AIStackAgent') $true 'Managed sshd config must restrict logins to the approved account'
Assert-Equal $managedConfig.Contains('PasswordAuthentication no') $true 'Managed sshd config must be key-only before opening its listener'
Assert-Equal $managedConfig.Contains('ListenAddress 100.64.10.2') $true 'Managed sshd config must bind to the host tailnet address'
Assert-Equal $managedConfig.Contains('ListenAddress fd7a:115c:a1e0::2') $true 'Managed sshd config must preserve a verified IPv6 tailnet listener when present'
Assert-Equal $managedConfig.Contains('ListenAddress 0.0.0.0') $false 'Managed sshd config must not bind all IPv4 interfaces'
Assert-NotContains $managedConfig 'AllowUsers *' 'Managed sshd config must not add a wildcard login grant'
$invalidListenRejected = $false
try { [void](New-RemoteSshManagedConfig -Port 2222 -WindowsAccount 'AIStackAgent' -ListenAddresses @('0.0.0.0')) } catch { $invalidListenRejected = $true }
Assert-Equal $invalidListenRejected $true 'Managed sshd config must reject non-tailnet listener addresses'
$invalidAccountRejected = $false
try { [void](New-RemoteSshManagedConfig -Port 2222 -WindowsAccount 'AIStackAgent;AllowUsers *' -ListenAddresses @('100.64.10.2')) } catch { $invalidAccountRejected = $true }
Assert-Equal $invalidAccountRejected $true 'Managed sshd config must reject account-line injection'

$knownDefaultRule = @{
    name = 'OpenSSH-Server-In-TCP'
    enabled = $true
    direction = 'Inbound'
    action = 'Allow'
    protocol = 'TCP'
    local_port = '22'
    remote_addresses = @('Any')
    program = '%SystemRoot%\System32\OpenSSH\sshd.exe'
    service = 'sshd'
}
Assert-Equal (Test-RemoteSshDefaultFirewallRule -Rule $knownDefaultRule) $true 'Only the exact known Windows OpenSSH default rule may be classified as the feature rule'
$knownDefaultRule.enabled = $false
Assert-Equal (Test-RemoteSshDefaultFirewallRule -Rule $knownDefaultRule -AllowDisabled) $true 'The exact feature-managed Windows rule may be recognized after disabling it'
$knownDefaultRule.enabled = $true
$lookalikeDefaultRule = $knownDefaultRule.Clone()
$lookalikeDefaultRule.name = 'Custom-SSH-Allow'
Assert-Equal (Test-RemoteSshDefaultFirewallRule -Rule $lookalikeDefaultRule) $false 'A custom rule must never be changed as if it were the Windows default'

$firewallSnapshot = @{
    enabled = 'True'; direction = 'Inbound'; action = 'Allow'; profile = 'Any'; protocol = 'TCP'; local_port = '2222'
    remote_addresses = @('100.64.10.20'); local_addresses = @('100.64.10.2')
    interface_aliases = @('Tailscale'); program = 'C:\Windows\System32\OpenSSH\sshd.exe'
}
Assert-Equal (Test-RemoteAccessFirewallRuleSnapshot -Actual $firewallSnapshot -Expected $firewallSnapshot) $true 'A feature firewall rule matching its recorded state may be rolled back'
$normalizedFirewallSnapshot = $firewallSnapshot.Clone()
$normalizedFirewallSnapshot.remote_addresses = @('100.64.10.20/32')
$normalizedFirewallSnapshot.local_addresses = @('100.64.10.2/32')
Assert-Equal (Test-RemoteAccessFirewallRuleSnapshot -Actual $normalizedFirewallSnapshot -Expected $firewallSnapshot) $true 'Windows firewall snapshots may normalize an exact IP address to its full-width prefix'
$modifiedFirewallSnapshot = $firewallSnapshot.Clone()
$modifiedFirewallSnapshot.remote_addresses = @('Any')
Assert-Equal (Test-RemoteAccessFirewallRuleSnapshot -Actual $modifiedFirewallSnapshot -Expected $firewallSnapshot) $false 'Rollback must preserve a feature rule that was broadened or locally edited'

$repeatFixture = New-SshPreflightFixture
$firstPlanJson = ConvertTo-Json (Get-RemoteSshInstallPlan $repeatFixture) -Depth 5 -Compress
$secondPlanJson = ConvertTo-Json (Get-RemoteSshInstallPlan $repeatFixture) -Depth 5 -Compress
Assert-Equal $firstPlanJson $secondPlanJson 'Repeated dry-run planning must not change local state or output'

$ownedManifest = @{ resources = @{ sshd_config = @{ managed = $true }; ssh_service = @{ managed = $false } } }
Assert-Equal (Test-RemoteAccessResourceOwned -Manifest $ownedManifest -Name 'sshd_config') $true 'Rollback may restore a resource recorded as feature-managed'
Assert-Equal (Test-RemoteAccessResourceOwned -Manifest $ownedManifest -Name 'ssh_service') $false 'Rollback must preserve an unchanged pre-existing service'
Assert-Equal (Test-RemoteAccessResourceOwned -Manifest $ownedManifest -Name 'unlisted_resource') $false 'Rollback must refuse resources absent from the feature manifest'
$rollbackPlan = Get-RemoteAccessRollbackPlan -Manifest $ownedManifest -Component Ssh
Assert-Equal $rollbackPlan.ready $true 'Rollback plan should include resources recorded as feature-managed'
Assert-Equal ($rollbackPlan.managed_resources -contains 'sshd_config') $true 'Rollback plan should restore a feature-managed config'
Assert-Equal ($rollbackPlan.managed_resources -contains 'ssh_service') $false 'Rollback plan must omit a pre-existing service'

Write-Output 'PASS: remote-access status fixtures are sanitized and fail closed.'
