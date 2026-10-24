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

$fallbackNow = [DateTimeOffset]::Parse('2026-10-24T12:00:00Z')
$fallbackAttestation = @{
    schema_version = 1; component = 'human-fallback'; method = 'rustdesk-direct-ip'; state = 'ready'
    tailnet_only = $true; public_relay_disabled = $true; credential_configured = $true
    external_access_blocked = $true; rdc_failure_recovered = $true; last_tested_utc = '2026-10-20T12:00:00Z'
}
Assert-Equal (Resolve-RemoteAccessHumanFallbackState -Attestation $fallbackAttestation -NowUtc $fallbackNow) 'ready' 'A recent operator-attested, tailnet-only fallback recovery should be ready'
$disabledFallbackAttestation = $fallbackAttestation.Clone()
$disabledFallbackAttestation.state = 'disabled'
Assert-Equal (Resolve-RemoteAccessHumanFallbackState -Attestation $disabledFallbackAttestation -NowUtc $fallbackNow) 'disabled' 'An explicit valid disabled fallback record should report disabled'
Assert-Equal (Resolve-RemoteAccessHumanFallbackState -Attestation $null -NowUtc $fallbackNow) 'unknown' 'A missing fallback record must remain unknown'
$staleFallbackAttestation = $fallbackAttestation.Clone()
$staleFallbackAttestation.last_tested_utc = '2026-07-01T12:00:00Z'
Assert-Equal (Resolve-RemoteAccessHumanFallbackState -Attestation $staleFallbackAttestation -NowUtc $fallbackNow) 'unknown' 'A stale fallback acceptance must return to unknown'
$publicRelayFallback = $fallbackAttestation.Clone()
$publicRelayFallback.public_relay_disabled = $false
Assert-Equal (Resolve-RemoteAccessHumanFallbackState -Attestation $publicRelayFallback -NowUtc $fallbackNow) 'unknown' 'A fallback using a public relay must never report ready'

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
Assert-Equal (ConvertTo-RemoteAccessFirewallState -Rules ([object[]]@()) -DefaultInboundAction 'Block' -ListenerProgramPaths @()) 'blocked' 'A successful firewall inspection with no allow rules and block-by-default is blocked'
Assert-Equal (ConvertTo-RemoteAccessFirewallState -Rules ([object[]]@()) -DefaultInboundAction 'unknown' -ListenerProgramPaths @()) 'unknown' 'An unknown default firewall action cannot be reported as blocked'

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
Assert-Equal ((ConvertFrom-RemoteAccessFirewallLocalPorts '*') -join ',') 'Any' 'The COM wildcard port representation must normalize to any port'
Assert-Equal ((ConvertFrom-RemoteAccessFirewallLocalPorts '') -join ',') 'Any' 'An empty COM port filter must normalize to any port'
Assert-Equal ((ConvertFrom-RemoteAccessFirewallLocalPorts '  ') -join ',') 'Any' 'A whitespace COM port filter must normalize to any port'
Assert-Equal ((ConvertFrom-RemoteAccessFirewallLocalPorts '22,2200-2300') -join ',') '22,2200-2300' 'Explicit COM ports and ranges must remain precise'
Assert-Equal (Get-RemoteAccessFirewallProfileState @([pscustomobject]@{ Enabled = $true; DefaultInboundAction = 'Block' })) 'blocked' 'A successfully inspected enabled block-by-default profile is safe to add a scoped rule to'
Assert-Equal (Get-RemoteAccessFirewallProfileState @([pscustomobject]@{ Enabled = $false; DefaultInboundAction = 'Block' }, [pscustomobject]@{ Enabled = $true; DefaultInboundAction = 'Block' })) 'unknown' 'A disabled profile must not be ignored when proving firewall enforcement'
Assert-Equal (Get-RemoteAccessFirewallProfileState @([pscustomobject]@{ Enabled = $true; DefaultInboundAction = 'Allow' })) 'broad' 'An enabled allow-by-default profile must be classified as broad'
$portQueryFailed = Get-RemoteAccessPortUseState -ListenerQuery { throw 'CIM unavailable' } -FeatureOwnsListener $false -ExpectedProcessName 'sshd'
Assert-Equal $portQueryFailed $null 'A failed listener query must remain unknown rather than report a free port'
$portQueryEmpty = Get-RemoteAccessPortUseState -ListenerQuery { @() } -FeatureOwnsListener $false -ExpectedProcessName 'sshd'
Assert-Equal $portQueryEmpty $false 'A successful empty listener query may report a free port'
$scopedAndWildcardComRules = @(
    @{ local_port = @('2222'); protocol = '6'; program = 'Any'; service = ''; remote_addresses = @('100.64.10.20'); interface_aliases = @('Tailscale') },
    @{ local_port = ConvertFrom-RemoteAccessFirewallLocalPorts '*'; protocol = '6'; program = 'Any'; service = ''; remote_addresses = @('Any'); interface_aliases = @() }
)
Assert-Equal (ConvertTo-RemoteAccessFirewallState -Rules $scopedAndWildcardComRules -DefaultInboundAction 'Block' -ListenerProgramPaths @() -Port 2222) 'broad' 'A COM wildcard allow must override an otherwise scoped firewall rule'

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
        host_key_path_safe = $true
        custom_sshd_config = $false
        authorized_key_exists = $false
        service_state = 'missing'
        firewall_state = 'blocked'
        firewall_default_allow = $false
        firewall_admin_context = $true
        firewall_rule_present = $false
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

$unknownSshFirewall = New-SshPreflightFixture
$unknownSshFirewall.firewall_state = 'unknown'
$unknownSshFirewallPlan = Get-RemoteSshInstallPlan $unknownSshFirewall
Assert-Equal $unknownSshFirewallPlan.ready $false 'Unavailable firewall inspection must stop SSH preflight before any mutation'
Assert-Equal ($unknownSshFirewallPlan.actions -contains 'create-scoped-firewall-rule') $false 'Unknown firewall inspection must not authorize a scoped-rule mutation'
$blockedSshFirewall = New-SshPreflightFixture
$blockedSshFirewall.firewall_state = 'blocked'
$blockedSshFirewallPlan = Get-RemoteSshInstallPlan $blockedSshFirewall
Assert-Equal $blockedSshFirewallPlan.ready $true 'A fully inspected block-by-default policy may proceed to create the scoped SSH rule'
Assert-Equal ($blockedSshFirewallPlan.actions -contains 'create-scoped-firewall-rule') $true 'A verified blocked firewall policy should plan the exact SSH allow rule'

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
Assert-Equal $managedConfig.Contains('AllowUsers aistackagent') $true 'Managed sshd config must use Windows OpenSSH lowercase account syntax'
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
Assert-Equal (Test-RemoteSshDefaultRuleForApply -Rule $knownDefaultRule -Resource @{ managed = $true; existed_before = $false }) $true 'An exact feature-owned disabled default rule must remain valid during idempotent reapply'
Assert-Equal (Test-RemoteSshDefaultRuleForApply -Rule $knownDefaultRule -Resource @{ managed = $false; existed_before = $true }) $false 'A disabled pre-existing default rule must not be treated as feature-owned'
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
$validLocalAccountSid = 'S-1-5-21-100-200-300-1001'
Assert-Equal (Test-RemoteAccessLocalAccountSid $validLocalAccountSid) $true 'A complete local account SID including its RID must pass rollback validation'
Assert-Equal (Test-RemoteAccessLocalAccountSid 'S-1-5-21-100-200-300') $false 'A machine-domain SID without an account RID must be rejected'
Assert-Equal (Test-RemoteAccessLocalAccountSid 'S-1-5-18') $false 'A built-in service SID must not be accepted as the approved SSH account'
Assert-Equal (Test-RemoteAccessLocalAccountIdentity -Sid $validLocalAccountSid -ExpectedName 'operator' -LocalAccount $true -ActualName 'OPERATOR') $true 'Rollback must bind the full local SID to its local account record'
Assert-Equal (Test-RemoteAccessLocalAccountIdentity -Sid $validLocalAccountSid -ExpectedName 'operator' -LocalAccount $false -ActualName 'operator') $false 'A domain account record must not satisfy the local SSH identity check'
Assert-Equal (Test-RemoteAccessLocalAccountIdentity -Sid $validLocalAccountSid -ExpectedName 'operator' -LocalAccount $true -ActualName 'other') $false 'Rollback must reject an account-name mismatch even when a SID-shaped value is supplied'
$roundTrippedSshManifest = '{"state":"rolling_back"}' | ConvertFrom-Json
Set-RemoteAccessManifestProperty -Manifest $roundTrippedSshManifest -Name 'rolled_back_at_utc' -Value '2026-10-24T12:00:00Z'
Assert-Equal $roundTrippedSshManifest.rolled_back_at_utc '2026-10-24T12:00:00Z' 'Rollback completion fields must be added to a JSON-round-tripped PSCustomObject'
Assert-Equal (Get-RemoteAccessManifestFailureState -PreviousState 'active' -MutationStarted $false) 'active' 'A failed reapply before host mutation must preserve an active manifest'
Assert-Equal (Get-RemoteAccessManifestFailureState -PreviousState 'active' -MutationStarted $true) 'applying' 'A failed reapply after a host mutation must remain visibly applying'
$hostKeyPlan = Get-RemoteSshHostKeySetupPlan -DirectorySafe $true -ExistingHostKeys @()
Assert-Equal $hostKeyPlan.ready $true 'A safe missing OpenSSH key directory can be initialized during explicit apply'
Assert-Equal $hostKeyPlan.create_directory $true 'First-use SSH setup must explicitly create its protected data directory'
Assert-Equal ($hostKeyPlan.missing_keys -contains 'ssh_host_ed25519_key') $true 'First-use SSH setup must plan a missing host identity key before configuration validation'
Assert-Equal (Get-RemoteSshHostKeySetupPlan -DirectorySafe $false -ExistingHostKeys @()).ready $false 'An unsafe or reparse-point SSH key directory must block bootstrap'
$rollbackPlan = Get-RemoteAccessRollbackPlan -Manifest $ownedManifest -Component Ssh
Assert-Equal $rollbackPlan.ready $true 'Rollback plan should include resources recorded as feature-managed'
Assert-Equal ($rollbackPlan.managed_resources -contains 'sshd_config') $true 'Rollback plan should restore a feature-managed config'
Assert-Equal ($rollbackPlan.managed_resources -contains 'ssh_service') $false 'Rollback plan must omit a pre-existing service'

$rdcConfig = New-RemoteAccessRdcConfig -Port 7770 -BindAddress '100.64.12.34' -AllowedIdentities @('operator@example.invalid') -ViewOnlyIdentities @('monitor@example.invalid')
Assert-Equal $rdcConfig.Contains('bind = "100.64.12.34"') $true 'rdc config must bind only to the verified tailnet address'
Assert-Equal $rdcConfig.Contains('can = ["view", "input"]') $true 'Control identities must receive view and input capabilities only'
Assert-Equal $rdcConfig.Contains('can = ["view"]') $true 'View-only identities must receive view capability only'
Assert-Equal $rdcConfig.Contains('clipboard') $false 'rdc clipboard capability must be absent by default'
Assert-Equal $rdcConfig.Contains('enabled = true') $true 'rdc auditing must be explicitly enabled'
Assert-Equal $rdcConfig.Contains('who = "*"') $false 'rdc config must never grant a wildcard identity'
$emptyRdcIdentityRejected = $false
try { [void](New-RemoteAccessRdcConfig -Port 7770 -BindAddress '100.64.12.34' -AllowedIdentities @('')) } catch { $emptyRdcIdentityRejected = $true }
Assert-Equal $emptyRdcIdentityRejected $true 'An empty identity must be rejected before config generation'
$wildcardRdcIdentityRejected = $false
try { [void](New-RemoteAccessRdcConfig -Port 7770 -BindAddress '100.64.12.34' -AllowedIdentities @('*')) } catch { $wildcardRdcIdentityRejected = $true }
Assert-Equal $wildcardRdcIdentityRejected $true 'A wildcard identity must be rejected before config generation'
$overlapRdcIdentityRejected = $false
try { [void](New-RemoteAccessRdcConfig -Port 7770 -BindAddress '100.64.12.34' -AllowedIdentities @('operator@example.invalid') -ViewOnlyIdentities @('OPERATOR@example.invalid')) } catch { $overlapRdcIdentityRejected = $true }
Assert-Equal $overlapRdcIdentityRejected $true 'An identity must not receive conflicting input and view-only grants'

Assert-Equal (Test-RemoteAccessRdcArchiveHash -ActualHash ('A' * 64) -ExpectedHash ('a' * 64)) $true 'Archive hashes should compare case-insensitively'
Assert-Equal (Test-RemoteAccessRdcArchiveHash -ActualHash ('b' * 64) -ExpectedHash ('a' * 64)) $false 'A mismatched rdc archive hash must block installation'
Assert-Equal (Test-RemoteAccessRdcArchiveHash -ActualHash 'not-a-hash' -ExpectedHash ('a' * 64)) $false 'Malformed archive hashes must fail closed'

Assert-Equal (Test-RemoteAccessRdcConfigAcl -IsProtected $true -WriteSids @('S-1-5-21-100-200-300-1001', 'S-1-5-18', 'S-1-5-32-544') -OwnerSid 'S-1-5-21-100-200-300-1001' -AllowedSids @('S-1-5-21-100-200-300-1001', 'S-1-5-18', 'S-1-5-32-544')) $true 'A protected config ACL limited to the owner, SYSTEM and Administrators should pass'
Assert-Equal (Test-RemoteAccessRdcConfigAcl -IsProtected $false -WriteSids @('S-1-5-21-100-200-300-1001') -OwnerSid 'S-1-5-21-100-200-300-1001' -AllowedSids @('S-1-5-21-100-200-300-1001')) $false 'An inheriting rdc config ACL must be rejected'
Assert-Equal (Test-RemoteAccessRdcConfigAcl -IsProtected $true -WriteSids @('S-1-1-0') -OwnerSid 'S-1-5-21-100-200-300-1001' -AllowedSids @('S-1-5-21-100-200-300-1001', 'S-1-5-18', 'S-1-5-32-544')) $false 'A config ACL writable by Everyone must be rejected'
Assert-Equal (Test-RemoteAccessRdcConfigAcl -IsProtected $true -WriteSids @('S-1-5-21-100-200-300-1001') -OwnerSid 'S-1-5-21-999-999-999-1001' -AllowedSids @('S-1-5-21-100-200-300-1001')) $false 'An unexpected config owner must be rejected'

$validRdcTask = @{
    name = 'dev.rdc.daemon.operator'
    expected_name = 'dev.rdc.daemon.operator'
    path = '\'
    expected_task_path = '\'
    action_count = 1
    state = 'Running'
    user_sid = 'S-1-5-21-100-200-300-1001'
    expected_user_sid = 'S-1-5-21-100-200-300-1001'
    run_level = 'Limited'
    logon_type = 'Interactive'
    executable = 'C:\Users\operator\AppData\Local\Programs\rdc\rdc.exe'
    expected_executable = 'C:\Users\operator\AppData\Local\Programs\rdc\rdc.exe'
    arguments = 'serve'
}
Assert-Equal (Test-RemoteAccessRdcTask -Task $validRdcTask) $true 'The pinned per-user logon task should be accepted at standard integrity'
$wrongTaskPathRdcTask = $validRdcTask.Clone()
$wrongTaskPathRdcTask.path = '\Unexpected\'
Assert-Equal (Test-RemoteAccessRdcTask -Task $wrongTaskPathRdcTask) $false 'A task moved outside its manifest-recorded task path must be rejected'
$missingTaskPathRdcTask = $validRdcTask.Clone()
$missingTaskPathRdcTask.path = ''
Assert-Equal (Test-RemoteAccessRdcTask -Task $missingTaskPathRdcTask) $false 'A task without a scheduler path must not count as ready'
$multipleActionsRdcTask = $validRdcTask.Clone()
$multipleActionsRdcTask.action_count = 2
Assert-Equal (Test-RemoteAccessRdcTask -Task $multipleActionsRdcTask) $false 'A task with additional actions must not count as ready or feature-owned'
$elevatedRdcTask = $validRdcTask.Clone()
$elevatedRdcTask.run_level = 'Highest'
Assert-Equal (Test-RemoteAccessRdcTask -Task $elevatedRdcTask) $false 'An elevated rdc task must be reported unavailable'
$wrongUserRdcTask = $validRdcTask.Clone()
$wrongUserRdcTask.user_sid = 'S-1-5-21-999-999-999-1001'
Assert-Equal (Test-RemoteAccessRdcTask -Task $wrongUserRdcTask) $false 'A task owned by another account must not count as desktop readiness'
$elevatedArgumentRdcTask = $validRdcTask.Clone()
$elevatedArgumentRdcTask.arguments = 'service install --elevated'
Assert-Equal (Test-RemoteAccessRdcTask -Task $elevatedArgumentRdcTask) $false 'A task invoking elevated rdc mode must be rejected'
$scopeOverrideRdcTask = $validRdcTask.Clone()
$scopeOverrideRdcTask.arguments = 'serve --allow "*"'
Assert-Equal (Test-RemoteAccessRdcTask -Task $scopeOverrideRdcTask) $false 'A task adding command-line identity grants must be rejected'
Assert-Equal (Test-RemoteAccessRdcRollbackTaskState -TaskPresent $true -TaskMatchesManifest $true -TaskManaged $true -ExpectedTaskPath '\' -ActualTaskPath '\') $true 'Rollback may remove a freshly verified task at the manifest path'
Assert-Equal (Test-RemoteAccessRdcRollbackTaskState -TaskPresent $true -TaskMatchesManifest $true -TaskManaged $true -ExpectedTaskPath '\' -ActualTaskPath '\Unexpected\') $false 'Rollback must reject a task whose path changed after confirmation'
Assert-Equal (Test-RemoteAccessRdcRollbackTaskState -TaskPresent $false -TaskMatchesManifest $true -TaskManaged $true -ExpectedTaskPath '\' -ActualTaskPath $null) $true 'A missing managed task may be cleaned up when no matching daemon process remains'
$applyingRdcManifest = '{"state":"applying","resources":{"task":{"managed":true,"task_path":null}}}' | ConvertFrom-Json
$registeredRdcTaskFacts = [pscustomobject]@{ valid = $true; task = [pscustomobject]@{ TaskPath = '\' } }
Set-RemoteAccessRdcTaskPath -Manifest $applyingRdcManifest -TaskFacts $registeredRdcTaskFacts | Out-Null
$persistedRdcManifest = ($applyingRdcManifest | ConvertTo-Json -Depth 6) | ConvertFrom-Json
Assert-Equal $persistedRdcManifest.resources.task.task_path '\' 'The validated task path must be persisted immediately after registration for partial-install rollback'
Assert-Equal (Test-RemoteAccessRdcRollbackTaskState -TaskPresent $true -TaskMatchesManifest $true -TaskManaged $true -ExpectedTaskPath $persistedRdcManifest.resources.task.task_path -ActualTaskPath '\') $true 'A task registered before listener failure must remain identifiable by rollback'
Assert-Equal (Test-RemoteAccessRdcListener -Listeners @(@{ local_address = '100.64.12.34'; owning_process = 4432 }) -RdcProcessIds @(4432) -TailnetAddresses @('100.64.12.34')) $true 'rdc readiness requires its own listener on a tailnet address'
Assert-Equal (Test-RemoteAccessRdcListener -Listeners @(@{ local_address = '0.0.0.0'; owning_process = 4432 }) -RdcProcessIds @(4432) -TailnetAddresses @('100.64.12.34')) $false 'rdc readiness must reject a wildcard listener'

function New-RdcInstallFixture {
    return @{
        identities_valid = $true
        archive_hash_valid = $true
        config_acl_valid = $true
        user_paths_safe = $true
        session_state = 'interactive'
        current_session_id = 3
        process_is_elevated = $false
        bind_address_valid = $true
        port_available = $true
        ssh_manifest_active = 'active'
        ssh_acceptance_attested = $true
        boot_recovery_ready = $true
        firewall_state = 'blocked'
        firewall_default_allow = $false
        firewall_admin_context = $true
        install_state = 'absent'
        task_valid = $false
        task_running = $false
    }
}

$signedOutRdc = New-RdcInstallFixture
$signedOutRdc.session_state = 'signed-out'
Assert-Equal ((Get-RemoteAccessRdcInstallPlan $signedOutRdc).blockers -contains 'interactive-session-required') $true 'rdc setup must refuse a signed-out invocation'
$unacceptedSshRdc = New-RdcInstallFixture
$unacceptedSshRdc.ssh_acceptance_attested = $false
Assert-Equal ((Get-RemoteAccessRdcInstallPlan $unacceptedSshRdc).blockers -contains 'ssh-recovery-acceptance-required') $true 'rdc setup must wait for an attested SSH recovery path'
$unsafeFirewallRdc = New-RdcInstallFixture
$unsafeFirewallRdc.firewall_state = 'broad'
Assert-Equal ((Get-RemoteAccessRdcInstallPlan $unsafeFirewallRdc).blockers -contains 'rdc-firewall-already-broad') $true 'A broad existing rule for the rdc port must block task startup'
$unknownFirewallRdc = New-RdcInstallFixture
$unknownFirewallRdc.firewall_state = 'unknown'
Assert-Equal (Get-RemoteAccessRdcInstallPlan $unknownFirewallRdc).ready $false 'Unavailable rdc firewall inspection must block installation before a manifest or resource is created'
$safeRdc = New-RdcInstallFixture
$safeRdcPlan = Get-RemoteAccessRdcInstallPlan $safeRdc
Assert-Equal $safeRdcPlan.ready $true 'A verified interactive standard-user desktop may be planned after SSH acceptance'
Assert-Equal ($safeRdcPlan.actions -contains 'register-standard-user-logon-task') $true 'The rdc install plan should explicitly register the standard-user logon task'
$repeatRdc = New-RdcInstallFixture
$repeatRdc.install_state = 'active-identical'
$repeatRdc.task_valid = $true
$repeatRdc.task_running = $true
$repeatRdc.firewall_rule_present = $true
$repeatRdcPlan = Get-RemoteAccessRdcInstallPlan $repeatRdc
Assert-Equal ($repeatRdcPlan.actions -contains 'register-standard-user-logon-task') $false 'A repeated identical install must not restart or re-register the healthy rdc task'
$repeatWithoutAdmin = New-RdcInstallFixture
$repeatWithoutAdmin.install_state = 'active-identical'
$repeatWithoutAdmin.task_valid = $true
$repeatWithoutAdmin.task_running = $true
$repeatWithoutAdmin.firewall_rule_present = $true
$repeatWithoutAdmin.firewall_admin_context = $false
Assert-Equal (Get-RemoteAccessRdcInstallPlan $repeatWithoutAdmin).ready $true 'A healthy repeated install needs no administrator token when its firewall rule already exists'

Assert-Equal (Test-RemoteAccessRdcInstallTaskState -InstallState 'active-identical' -TaskPresent $false -TaskValid $false) $true 'A missing feature-managed task may be repaired during an identical reinstall'
Assert-Equal (Test-RemoteAccessRdcInstallTaskState -InstallState 'active-identical' -TaskPresent $true -TaskValid $false) $false 'A changed feature-managed task must block reinstall'
Assert-Equal (Test-RemoteAccessRdcInstallTaskState -InstallState 'absent' -TaskPresent $true -TaskValid $false) $false 'An unmanaged task must block initial registration'

$dryRunTaskRegistrations = 0
$dryRunElevationAttempts = 0
Invoke-RemoteAccessRdcMutation -Apply:$false -Mutation { $dryRunTaskRegistrations++; $dryRunElevationAttempts++ } | Out-Null
Assert-Equal $dryRunTaskRegistrations 0 'Dry-run rdc installation must not register a scheduled task'
Assert-Equal $dryRunElevationAttempts 0 'Dry-run rdc installation must not request elevation'

$rdcRollbackManifest = @{
    schema_version = 1; component = 'rdc'; state = 'active'
    resources = @{
        task = @{ managed = $true }
        firewall = @{ managed = $true }
        config = @{ managed = $true; sha256 = 'config-hash' }
        binary = @{ managed = $true; sha256 = 'binary-hash' }
    }
}
$rdcRollbackPlan = Get-RemoteAccessRdcRollbackPlan -Manifest $rdcRollbackManifest -CurrentBinaryHash 'binary-hash' -CurrentConfigHash 'config-hash' -TaskMatchesManifest $true -FirewallMatchesManifest $true
Assert-Equal $rdcRollbackPlan.ready $true 'An unchanged active rdc manifest should be eligible for rollback'
Assert-Equal ($rdcRollbackPlan.managed_resources -contains 'task') $true 'rdc rollback should include its feature-managed logon task'
$changedRdcRollback = Get-RemoteAccessRdcRollbackPlan -Manifest $rdcRollbackManifest -CurrentBinaryHash 'edited-binary' -CurrentConfigHash 'config-hash' -TaskMatchesManifest $true -FirewallMatchesManifest $true
Assert-Equal ($changedRdcRollback.blockers -contains 'rdc-binary-changed') $true 'rdc rollback must preserve an edited binary'
$driftedRdcRollback = Get-RemoteAccessRdcRollbackPlan -Manifest $rdcRollbackManifest -CurrentBinaryHash 'binary-hash' -CurrentConfigHash 'config-hash' -TaskMatchesManifest $false -FirewallMatchesManifest $false
Assert-Equal ($driftedRdcRollback.blockers -contains 'rdc-task-scope-changed') $true 'rdc rollback must refuse a task that no longer matches its manifest'
Assert-Equal ($driftedRdcRollback.blockers -contains 'rdc-firewall-scope-changed') $true 'rdc rollback must refuse a firewall rule that no longer matches its manifest'
$orphanRdcProcess = Get-RemoteAccessRdcRollbackPlan -Manifest $rdcRollbackManifest -CurrentBinaryHash 'binary-hash' -CurrentConfigHash 'config-hash' -TaskMatchesManifest $true -FirewallMatchesManifest $true -TaskPresent $false -ProcessRunning $true
Assert-Equal ($orphanRdcProcess.blockers -contains 'rdc-orphan-process-running') $true 'rdc rollback must block file removal while its daemon runs without a registered task'

Write-Output 'PASS: remote-access status fixtures are sanitized and fail closed.'
