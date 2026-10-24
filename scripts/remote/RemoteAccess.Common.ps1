#Requires -Version 5.1

function Get-RemoteAccessField {
    param(
        [AllowNull()][object]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($null -eq $InputObject) { return $null }
    if ($InputObject -is [System.Collections.IDictionary]) {
        if ($InputObject.Contains($Name)) { return $InputObject[$Name] }
        return $null
    }
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -ne $property) { return $property.Value }
    return $null
}

function Set-RemoteAccessManifestProperty {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Manifest,
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()][object]$Value
    )

    if ($Manifest -is [System.Collections.IDictionary]) {
        $Manifest[$Name] = $Value
        return
    }
    $property = $Manifest.PSObject.Properties[$Name]
    if ($null -ne $property) { $property.Value = $Value; return }
    $Manifest | Add-Member -MemberType NoteProperty -Name $Name -Value $Value
}

function Test-RemoteAccessLocalAccountSid {
    [CmdletBinding()]
    param([AllowNull()][string]$Sid)

    if ([string]::IsNullOrWhiteSpace($Sid)) { return $false }
    try {
        $parsed = New-Object Security.Principal.SecurityIdentifier($Sid)
        if ($null -eq $parsed.AccountDomainSid) { return $false }
        $parts = $parsed.Value.Split('-')
        return ($parts.Count -eq 8 -and $parts[0] -eq 'S' -and $parts[1] -eq '1' -and $parts[2] -eq '5' -and $parts[3] -eq '21')
    } catch { return $false }
}

function Test-RemoteAccessLocalAccountIdentity {
    [CmdletBinding()]
    param(
        [AllowNull()][string]$Sid,
        [AllowNull()][string]$ExpectedName,
        [AllowNull()][object]$LocalAccount,
        [AllowNull()][string]$ActualName
    )

    if (-not (Test-RemoteAccessLocalAccountSid $Sid)) { return $false }
    if ($LocalAccount -ne $true) { return $false }
    if ([string]::IsNullOrWhiteSpace($ExpectedName) -or [string]::IsNullOrWhiteSpace($ActualName)) { return $false }
    return ([string]$ExpectedName -ieq [string]$ActualName)
}

function Get-RemoteAccessManifestFailureState {
    [CmdletBinding()]
    param([AllowNull()][string]$PreviousState, [bool]$MutationStarted)

    if ($PreviousState -eq 'active' -and -not $MutationStarted) { return 'active' }
    return 'applying'
}

function Get-RemoteAccessSshStatusPort {
    [CmdletBinding()]
    param([AllowNull()][object]$Manifest)

    if ($null -eq $Manifest) { return 22 }
    if ((Get-RemoteAccessField $Manifest 'schema_version') -ne 1 -or (Get-RemoteAccessField $Manifest 'component') -ne 'ssh') { return $null }
    $state = [string](Get-RemoteAccessField $Manifest 'state')
    if ($state -eq 'rolled_back') { return 22 }
    if ($state -ne 'active') { return $null }
    try {
        $port = [int](Get-RemoteAccessField $Manifest 'port')
        if ($port -ge 1 -and $port -le 65535) { return $port }
    } catch { }
    return $null
}

function Test-RemoteSshManifestScope {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$Manifest,
        [Parameter(Mandatory = $true)][string]$AccountSid,
        [Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$Port,
        [Parameter(Mandatory = $true)][string[]]$ClientAddresses,
        [Parameter(Mandatory = $true)][string[]]$ListenerAddresses,
        [Parameter(Mandatory = $true)][string[]]$InterfaceAliases
    )

    if ($null -eq $Manifest) { return $true }
    if ((Get-RemoteAccessField $Manifest 'schema_version') -ne 1 -or (Get-RemoteAccessField $Manifest 'component') -ne 'ssh') { return $false }
    if ((Get-RemoteAccessField $Manifest 'state') -notin @('applying', 'active')) { return $false }
    $account = Get-RemoteAccessField $Manifest 'account'
    if ([string](Get-RemoteAccessField $account 'sid') -ne $AccountSid -or [int](Get-RemoteAccessField $Manifest 'port') -ne $Port) { return $false }

    foreach ($pair in @(
        @{ saved = (Get-RemoteAccessField $Manifest 'client_addresses'); requested = $ClientAddresses; address = $true },
        @{ saved = (Get-RemoteAccessField $Manifest 'listener_addresses'); requested = $ListenerAddresses; address = $true },
        @{ saved = (Get-RemoteAccessField $Manifest 'listener_interfaces'); requested = $InterfaceAliases; address = $false }
    )) {
        $normalizer = if ($pair.address) { { param($value) ConvertTo-RemoteAccessAddressKey ([string]$value) } } else { { param($value) [string]$value } }
        $saved = @($pair.saved | ForEach-Object { & $normalizer $_ } | Sort-Object -Unique)
        $requested = @($pair.requested | ForEach-Object { & $normalizer $_ } | Sort-Object -Unique)
        if ($saved.Count -ne $requested.Count) { return $false }
        for ($index = 0; $index -lt $saved.Count; $index++) {
            if ($saved[$index] -ine $requested[$index]) { return $false }
        }
    }
    return $true
}

function Get-RemoteAccessSshPort {
    $path = Join-Path $env:ProgramData 'AI-Stack\RemoteAccess\ssh-manifest.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return 22 }
    try {
        $manifest = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        return Get-RemoteAccessSshStatusPort -Manifest $manifest
    } catch {
        return $null
    }
}

function ConvertTo-RemoteAccessEnum {
    param(
        [AllowNull()][object]$Value,
        [Parameter(Mandatory = $true)][string[]]$Allowed
    )

    if ($Value -isnot [string]) { return 'unknown' }
    $normalized = $Value.Trim().ToLowerInvariant()
    if ($Allowed -contains $normalized) { return $normalized }
    return 'unknown'
}

function ConvertTo-RemoteAccessTimestamp {
    param([AllowNull()][object]$Value)

    if ($Value -isnot [string] -and $Value -isnot [DateTime] -and $Value -isnot [DateTimeOffset]) {
        return 'unknown'
    }
    try {
        $date = [DateTimeOffset]::Parse([string]$Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal)
        return $date.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)
    } catch {
        return 'unknown'
    }
}

function ConvertTo-RemoteAccessCount {
    param([AllowNull()][object]$Value)

    if ($null -eq $Value) { return $null }
    try {
        $count = [long]$Value
        if ($count -lt 0) { return $null }
        return $count
    } catch {
        return $null
    }
}

function Resolve-RemoteAccessHumanFallbackState {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$Attestation,
        [DateTimeOffset]$NowUtc = [DateTimeOffset]::UtcNow
    )

    if ($null -eq $Attestation -or (Get-RemoteAccessField $Attestation 'schema_version') -ne 1 -or (Get-RemoteAccessField $Attestation 'component') -ne 'human-fallback') { return 'unknown' }
    if ((Get-RemoteAccessField $Attestation 'method') -ne 'rustdesk-direct-ip') { return 'unknown' }
    $state = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $Attestation 'state') @('ready', 'disabled', 'unknown')
    if ($state -eq 'disabled') { return 'disabled' }
    if ($state -ne 'ready') { return 'unknown' }
    foreach ($field in @('tailnet_only', 'public_relay_disabled', 'credential_configured', 'external_access_blocked', 'rdc_failure_recovered')) {
        if ((Get-RemoteAccessField $Attestation $field) -ne $true) { return 'unknown' }
    }
    $testedAt = ConvertTo-RemoteAccessTimestamp (Get-RemoteAccessField $Attestation 'last_tested_utc')
    if ($testedAt -eq 'unknown') { return 'unknown' }
    try {
        $tested = [DateTimeOffset]::Parse($testedAt, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal)
        $age = $NowUtc.ToUniversalTime() - $tested.ToUniversalTime()
        if ($age.TotalMinutes -lt -5 -or $age.TotalDays -gt 90) { return 'unknown' }
    } catch { return 'unknown' }
    return 'ready'
}

function Get-RemoteAccessHumanFallbackState {
    $path = Join-Path $env:ProgramData 'AI-Stack\RemoteAccess\human-fallback-status.json'
    try {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return 'unknown' }
        if ((Get-Item -LiteralPath $path -Force -ErrorAction Stop).Attributes -band [IO.FileAttributes]::ReparsePoint) { return 'unknown' }
        $acl = Get-Acl -LiteralPath $path -ErrorAction Stop
        $writeMask = [int][Security.AccessControl.FileSystemRights]::Write -bor [int][Security.AccessControl.FileSystemRights]::Modify -bor [int][Security.AccessControl.FileSystemRights]::Delete -bor [int][Security.AccessControl.FileSystemRights]::ChangePermissions -bor [int][Security.AccessControl.FileSystemRights]::TakeOwnership -bor [int][Security.AccessControl.FileSystemRights]::FullControl
        $writers = New-Object 'System.Collections.Generic.List[string]'
        foreach ($rule in $acl.Access) {
            if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or (([int]$rule.FileSystemRights -band $writeMask) -eq 0)) { continue }
            $writers.Add($rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value)
        }
        $sid = [string]([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
        if (-not (Test-RemoteAccessRdcConfigAcl -IsProtected $acl.AreAccessRulesProtected -WriteSids $writers.ToArray() -OwnerSid (ConvertTo-RemoteAccessAccountSid ([string]$acl.Owner)) -AllowedSids @($sid, 'S-1-5-18', 'S-1-5-32-544'))) { return 'unknown' }
        $attestation = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        return Resolve-RemoteAccessHumanFallbackState -Attestation $attestation
    } catch {
        return 'unknown'
    }
}

function Test-RemoteAccessTailnetAddress {
    [CmdletBinding()]
    param([AllowNull()][string]$Address)

    if ([string]::IsNullOrWhiteSpace($Address)) { return $false }
    $parts = $Address.Trim().Split('/')
    if ($parts.Count -gt 2) { return $false }
    try {
        $ip = [Net.IPAddress]::Parse($parts[0])
        if ($ip.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) {
            $prefixLength = 32
            if ($parts.Count -eq 2) { $prefixLength = [int]$parts[1] }
            if ($prefixLength -lt 10 -or $prefixLength -gt 32) { return $false }
            $bytes = $ip.GetAddressBytes()
            return ($bytes[0] -eq 100 -and $bytes[1] -ge 64 -and $bytes[1] -le 127)
        }
        if ($ip.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6) {
            $prefixLength = 128
            if ($parts.Count -eq 2) { $prefixLength = [int]$parts[1] }
            if ($prefixLength -lt 48 -or $prefixLength -gt 128) { return $false }
            $bytes = $ip.GetAddressBytes()
            return ($bytes[0] -eq 0xfd -and $bytes[1] -eq 0x7a -and $bytes[2] -eq 0x11 -and $bytes[3] -eq 0x5c -and $bytes[4] -eq 0xa1 -and $bytes[5] -eq 0xe0)
        }
    } catch {
        return $false
    }
    return $false
}

function Test-RemoteSshClientAddress {
    [CmdletBinding()]
    param([AllowNull()][string]$Address)

    if ([string]::IsNullOrWhiteSpace($Address) -or $Address.Contains('/')) { return $false }
    try { [void][Net.IPAddress]::Parse($Address.Trim()) } catch { return $false }
    return Test-RemoteAccessTailnetAddress $Address
}

function ConvertFrom-RemoteOpenSshPublicKey {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$KeyText)

    $raw = $KeyText.Trim()
    if ($raw.Length -gt 16384 -or $raw.Contains("`r") -or $raw.Contains("`n")) { throw 'The public key must be one bounded OpenSSH key line.' }
    if ($raw -notmatch '^(?<type>ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521)\s+(?<data>[A-Za-z0-9+/]+={0,2})(?:\s+.*)?$') {
        throw 'The public key type or encoding is not supported.'
    }
    try { $bytes = [Convert]::FromBase64String($Matches.data) } catch { throw 'The public key payload is invalid.' }
    if ($bytes.Length -lt 5) { throw 'The public key payload is incomplete.' }
    $typeLength = ([int]$bytes[0] * 16777216) + ([int]$bytes[1] * 65536) + ([int]$bytes[2] * 256) + [int]$bytes[3]
    if ($typeLength -lt 1 -or $typeLength -gt 128 -or $typeLength -gt ($bytes.Length - 4)) { throw 'The public key payload has an invalid type field.' }
    $blobType = [Text.Encoding]::ASCII.GetString($bytes, 4, $typeLength)
    if ($blobType -cne $Matches.type) { throw 'The public key line type does not match its encoded key payload.' }
    return [pscustomobject]@{ type = $Matches.type; data = $Matches.data; line = ($Matches.type + ' ' + $Matches.data) }
}

function New-RemoteSshManagedConfig {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$Port,
        [Parameter(Mandatory = $true)][string]$WindowsAccount,
        [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string[]]$ListenAddresses
    )

    if ($WindowsAccount -notmatch '^[A-Za-z0-9_.-]+(?:\\[A-Za-z0-9_.-]+)?$') {
        throw 'The Windows account must be a simple local or domain account name.'
    }
    $openSshAccount = $WindowsAccount.ToLowerInvariant()
    if (@($ListenAddresses | Where-Object { -not (Test-RemoteAccessTailnetAddress $_) }).Count -gt 0) {
        throw 'The SSH listener addresses must be verified Tailscale addresses.'
    }
    $listenLines = @($ListenAddresses | Sort-Object -Unique | ForEach-Object { 'ListenAddress ' + $_ }) -join "`r`n"

    return @"
# Managed by AI-Stack remote-access setup. Local edits are detected and preserved for review.
Port $Port
AddressFamily any
$listenLines
PubkeyAuthentication yes
PasswordAuthentication no
PermitEmptyPasswords no
AllowUsers $openSshAccount
AuthorizedKeysFile .ssh/authorized_keys
Subsystem sftp sftp-server.exe
"@
}

function Get-RemoteSshHostKeySetupPlan {
    [CmdletBinding()]
    param([bool]$DirectorySafe, [bool]$DirectoryExists = $false, [AllowNull()][string[]]$ExistingHostKeys = @())

    $required = @('ssh_host_ecdsa_key', 'ssh_host_ed25519_key', 'ssh_host_rsa_key')
    $blockers = @()
    if (-not $DirectorySafe) { $blockers += 'ssh-host-key-path-unsafe' }
    $existing = @($ExistingHostKeys | ForEach-Object { [IO.Path]::GetFileName([string]$_) } | Sort-Object -Unique)
    $unknown = @($existing | Where-Object { $_ -notin $required })
    if ($unknown.Count -gt 0) { $blockers += 'ssh-host-key-inventory-invalid' }
    $missing = @($required | Where-Object { $_ -notin $existing })
    return [pscustomobject]@{
        ready = ($blockers.Count -eq 0)
        blockers = [string[]]$blockers
        create_directory = ($DirectorySafe -and -not $DirectoryExists)
        missing_keys = [string[]]$missing
        generate_missing = ($missing.Count -gt 0)
    }
}

function Test-RemoteSshDefaultFirewallRule {
    [CmdletBinding()]
    param([AllowNull()][object]$Rule, [switch]$AllowDisabled)

    if ($null -eq $Rule) { return $false }
    $name = [string](Get-RemoteAccessField $Rule 'name')
    $enabled = Get-RemoteAccessField $Rule 'enabled'
    $direction = [string](Get-RemoteAccessField $Rule 'direction')
    $action = [string](Get-RemoteAccessField $Rule 'action')
    $protocol = [string](Get-RemoteAccessField $Rule 'protocol')
    $localPort = [string](Get-RemoteAccessField $Rule 'local_port')
    $service = [string](Get-RemoteAccessField $Rule 'service')
    $remoteAddresses = @(Get-RemoteAccessField $Rule 'remote_addresses' | ForEach-Object { [string]$_ })
    $program = [Environment]::ExpandEnvironmentVariables([string](Get-RemoteAccessField $Rule 'program'))

    if ($name -ne 'OpenSSH-Server-In-TCP') { return $false }
    if ($enabled -ne $true -and -not ($AllowDisabled -and $enabled -eq $false)) { return $false }
    if ($direction -notin @('Inbound', '1') -or $action -notin @('Allow', '1')) { return $false }
    if ($protocol -notin @('TCP', '6') -or $localPort -ne '22' -or $service -notin @('sshd', 'Any', '*', '')) { return $false }
    if ($remoteAddresses.Count -ne 1 -or $remoteAddresses[0] -notin @('Any', '*')) { return $false }
    $expectedProgram = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'
    try {
        return ([IO.Path]::GetFullPath($program).TrimEnd('\') -ieq [IO.Path]::GetFullPath($expectedProgram).TrimEnd('\'))
    } catch {
        return $false
    }
}

function Test-RemoteSshDefaultRuleForApply {
    [CmdletBinding()]
    param([AllowNull()][object]$Rule, [AllowNull()][object]$Resource)

    $ownedNewRule = (Get-RemoteAccessField $Resource 'managed') -eq $true -and (Get-RemoteAccessField $Resource 'existed_before') -ne $true
    return (Test-RemoteSshDefaultFirewallRule -Rule $Rule -AllowDisabled:$ownedNewRule)
}

function Test-RemoteAccessFirewallRuleSnapshot {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$Actual,
        [Parameter(Mandatory = $true)][object]$Expected
    )

    foreach ($name in @('enabled', 'direction', 'action', 'profile', 'protocol', 'local_port')) {
        if ([string](Get-RemoteAccessField $Actual $name) -ine [string](Get-RemoteAccessField $Expected $name)) { return $false }
    }
    foreach ($name in @('remote_addresses', 'local_addresses', 'interface_aliases')) {
        $normalizer = { param($value) [string]$value }
        if ($name -ne 'interface_aliases') { $normalizer = { param($value) ConvertTo-RemoteAccessAddressKey ([string]$value) } }
        $actualSet = @((Get-RemoteAccessField $Actual $name) | ForEach-Object { & $normalizer $_ } | Sort-Object -Unique)
        $expectedSet = @((Get-RemoteAccessField $Expected $name) | ForEach-Object { & $normalizer $_ } | Sort-Object -Unique)
        if ($actualSet.Count -ne $expectedSet.Count) { return $false }
        for ($index = 0; $index -lt $actualSet.Count; $index++) {
            if ($actualSet[$index] -ine $expectedSet[$index]) { return $false }
        }
    }
    $actualProgram = [Environment]::ExpandEnvironmentVariables([string](Get-RemoteAccessField $Actual 'program'))
    $expectedProgram = [Environment]::ExpandEnvironmentVariables([string](Get-RemoteAccessField $Expected 'program'))
    try {
        return ([IO.Path]::GetFullPath($actualProgram).TrimEnd('\') -ieq [IO.Path]::GetFullPath($expectedProgram).TrimEnd('\'))
    } catch {
        return $false
    }
}

function ConvertTo-RemoteAccessAddressKey {
    param([AllowNull()][string]$Address)
    if ([string]::IsNullOrWhiteSpace($Address) -or $Address -in @('Any', '*')) { return [string]$Address }
    $parts = $Address.Trim().Split('/')
    try {
        $ip = [Net.IPAddress]::Parse($parts[0])
        if ($parts.Count -eq 1) { return $ip.ToString() }
        $prefix = [int]$parts[1]
        $fullLength = if ($ip.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) { 32 } else { 128 }
        if ($prefix -eq $fullLength) { return $ip.ToString() }
        return ($ip.ToString() + '/' + $prefix.ToString([Globalization.CultureInfo]::InvariantCulture))
    } catch {
        return $Address.Trim()
    }
}

function Get-RemoteAccessFirewallRuleSnapshot {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Rule)

    $port = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $Rule -ErrorAction Stop
    $address = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $Rule -ErrorAction Stop
    $app = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $Rule -ErrorAction Stop
    $interfaces = Get-NetFirewallInterfaceFilter -AssociatedNetFirewallRule $Rule -ErrorAction Stop
    return @{
        enabled = [string]$Rule.Enabled
        direction = [string]$Rule.Direction
        action = [string]$Rule.Action
        profile = [string]$Rule.Profile
        protocol = [string]$port.Protocol
        local_port = [string]$port.LocalPort
        remote_addresses = @($address.RemoteAddress | ForEach-Object { [string]$_ })
        local_addresses = @($address.LocalAddress | ForEach-Object { [string]$_ })
        interface_aliases = @($interfaces.InterfaceAlias | ForEach-Object { [string]$_ })
        program = [string]$app.Program
    }
}

function Test-RemoteAccessSshListener {
    [CmdletBinding()]
    param(
        [AllowNull()][object[]]$Listeners,
        [AllowNull()][int[]]$SshdProcessIds,
        [AllowNull()][string[]]$TailscaleAddresses
    )

    if (-not $Listeners -or -not $SshdProcessIds -or -not $TailscaleAddresses) { return $false }
    $sshPidSet = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($processId in $SshdProcessIds) { [void]$sshPidSet.Add([int]$processId) }
    $tailnetAddressSet = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($address in $TailscaleAddresses) {
        if (Test-RemoteAccessTailnetAddress $address) { [void]$tailnetAddressSet.Add((ConvertTo-RemoteAccessAddressKey $address)) }
    }
    if ($tailnetAddressSet.Count -eq 0) { return $false }
    foreach ($listener in $Listeners) {
        $processId = Get-RemoteAccessField $listener 'owning_process'
        $localAddress = [string](Get-RemoteAccessField $listener 'local_address')
        if ($null -eq $processId -or -not $sshPidSet.Contains([int]$processId)) { return $false }
        if (-not $tailnetAddressSet.Contains((ConvertTo-RemoteAccessAddressKey $localAddress))) { return $false }
    }
    return $true
}

function ConvertFrom-RemoteAccessFirewallLocalPorts {
    [CmdletBinding()]
    param([AllowNull()][object]$LocalPorts)

    $text = [string]$LocalPorts
    if ([string]::IsNullOrWhiteSpace($text) -or $text.Trim() -in @('*', 'Any')) { return @('Any') }
    $parts = @($text.Split(',') | ForEach-Object { $_.Trim() })
    if (-not $parts -or @($parts | Where-Object { [string]::IsNullOrWhiteSpace($_) }).Count -gt 0) { return @('Any') }
    return $parts
}

function Get-RemoteAccessFirewallProfileState {
    [CmdletBinding()]
    param([AllowNull()][object[]]$Profiles)

    if (-not $Profiles -or @($Profiles | Where-Object { [string]$_.Enabled -ne 'True' }).Count -gt 0) { return 'unknown' }
    $actions = @($Profiles | ForEach-Object { [string]$_.DefaultInboundAction.ToString() } | Sort-Object -Unique)
    if ($actions -contains 'Allow') { return 'broad' }
    if (@($actions | Where-Object { $_ -ne 'Block' }).Count -gt 0) { return 'unknown' }
    return 'blocked'
}

function Get-RemoteAccessPortUseState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][scriptblock]$ListenerQuery,
        [ValidateRange(1, 65535)][int]$Port = 22,
        [Parameter(Mandatory = $true)][bool]$FeatureOwnsListener,
        [Parameter(Mandatory = $true)][string]$ExpectedProcessName,
        [AllowNull()][string]$ExpectedExecutable
    )

    try { $listeners = @(& $ListenerQuery $Port) } catch { return $null }
    if (-not $listeners) { return $false }
    if (-not $FeatureOwnsListener) { return $true }
    foreach ($listener in $listeners) {
        try {
            $process = Get-Process -Id ([int](Get-RemoteAccessField $listener 'OwningProcess')) -ErrorAction Stop
            if ([string]$process.ProcessName -ine $ExpectedProcessName) { return $true }
            if (-not [string]::IsNullOrWhiteSpace($ExpectedExecutable) -and [IO.Path]::GetFullPath([string]$process.Path) -ine [IO.Path]::GetFullPath($ExpectedExecutable)) { return $true }
        } catch { return $null }
    }
    return $false
}

function ConvertTo-RemoteAccessFirewallState {
    [CmdletBinding()]
    param(
        [AllowNull()][object[]]$Rules,
        [AllowNull()][object]$DefaultInboundAction,
        [AllowNull()][string[]]$ListenerProgramPaths,
        [ValidateRange(1, 65535)][int]$Port = 22
    )

    $defaultAction = [string]$DefaultInboundAction
    if ($defaultAction -eq 'Allow' -or $defaultAction -eq '6') { return 'broad' }
    if ($defaultAction -ne 'Block') { return 'unknown' }
    if ($null -eq $Rules) { return 'unknown' }
    if (@($Rules).Count -eq 0) { return 'blocked' }
    $listenerPaths = @($ListenerProgramPaths | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $matchedRuleCount = 0
    $allTailnetScoped = $true

    foreach ($rule in $Rules) {
        $protocol = [string](Get-RemoteAccessField $rule 'protocol')
        if ($protocol -notin @('TCP', '6', 'Any', '256')) { continue }
        $localPorts = @(Get-RemoteAccessField $rule 'local_port' | ForEach-Object { [string]$_ })
        $program = [string](Get-RemoteAccessField $rule 'program')
        $service = [string](Get-RemoteAccessField $rule 'service')

        $isPort22 = $localPorts -contains [string]$Port
        $isAnyPort = $localPorts -contains 'Any'
        if (-not $isPort22 -and -not $isAnyPort) {
            foreach ($localPort in $localPorts) {
                if ($localPort -match '^(\d+)-(\d+)$' -and [int]$Matches[1] -le $Port -and [int]$Matches[2] -ge $Port) {
                    $isPort22 = $true
                    break
                }
            }
        }
        if (-not $isPort22 -and -not $isAnyPort) { continue }

        $programIsAny = [string]::IsNullOrWhiteSpace($program) -or $program -eq 'Any'
        $serviceIsSshd = $service -eq 'sshd'
        $programMatchesListener = $false
        if (-not $programIsAny) {
            $expandedProgram = [Environment]::ExpandEnvironmentVariables($program)
            foreach ($listenerPath in $listenerPaths) {
                try {
                    if ([IO.Path]::GetFullPath($expandedProgram).TrimEnd('\') -ieq [IO.Path]::GetFullPath($listenerPath).TrimEnd('\')) {
                        $programMatchesListener = $true
                        break
                    }
                } catch { }
            }
        }
        if (-not $programIsAny -and -not $programMatchesListener -and -not $serviceIsSshd) { continue }
        if ($isAnyPort -and -not $programIsAny -and -not $programMatchesListener -and -not $serviceIsSshd) { continue }

        $matchedRuleCount++
        $remoteAddresses = @(Get-RemoteAccessField $rule 'remote_addresses' | ForEach-Object { [string]$_ })
        if ($remoteAddresses.Count -eq 0 -or $remoteAddresses -contains 'Any') { return 'broad' }
        $interfaceAliases = @(Get-RemoteAccessField $rule 'interface_aliases' | ForEach-Object { [string]$_ })
        $interfaceIsTailnet = @($interfaceAliases | Where-Object { $_ -match '(?i)tailscale' }).Count -gt 0
        $allRemoteAddressesAreTailnet = $true
        foreach ($address in $remoteAddresses) {
            if (-not (Test-RemoteAccessTailnetAddress $address)) {
                $allRemoteAddressesAreTailnet = $false
                break
            }
        }
        if (-not ($interfaceIsTailnet -and $allRemoteAddressesAreTailnet)) { $allTailnetScoped = $false }
    }

    if ($matchedRuleCount -eq 0) { return 'unknown' }
    if ($allTailnetScoped) { return 'tailnet-only' }
    return 'broad'
}

function ConvertTo-RemoteAccessStatus {
    [CmdletBinding()]
    param([AllowNull()][object]$Inventory)

    $tailnet = Get-RemoteAccessField $Inventory 'tailnet'
    $ssh = Get-RemoteAccessField $Inventory 'ssh'
    $desktop = Get-RemoteAccessField $Inventory 'desktop'
    $hostInfo = Get-RemoteAccessField $Inventory 'host'

    $tailnetState = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $tailnet 'state') @('connected', 'disconnected', 'unknown')
    $interfacePresent = Get-RemoteAccessField $tailnet 'interface_present'
    $addressPresent = Get-RemoteAccessField $tailnet 'address_present'
    if ($tailnetState -eq 'connected' -and ($interfacePresent -ne $true -or $addressPresent -ne $true)) {
        $tailnetState = 'unknown'
    }

    $audit = Get-RemoteAccessField $desktop 'audit'
    $auditValid = (Get-RemoteAccessField $audit 'valid') -eq $true
    $auditCategory = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $audit 'category') @('authorization', 'view_only', 'other')
    $auditTimestamp = ConvertTo-RemoteAccessTimestamp (Get-RemoteAccessField $audit 'timestamp')
    if (-not $auditValid -or $auditCategory -eq 'unknown' -or $auditTimestamp -eq 'unknown') {
        $lastAudit = [pscustomobject]@{ timestamp = 'unknown'; category = 'unknown' }
        $denied = [pscustomobject]@{ authorization = $null; view_only = $null; other = $null }
    } else {
        $counts = Get-RemoteAccessField $desktop 'denied_by_category'
        $lastAudit = [pscustomobject]@{ timestamp = $auditTimestamp; category = $auditCategory }
        $denied = [pscustomobject]@{
            authorization = ConvertTo-RemoteAccessCount (Get-RemoteAccessField $counts 'authorization')
            view_only = ConvertTo-RemoteAccessCount (Get-RemoteAccessField $counts 'view_only')
            other = ConvertTo-RemoteAccessCount (Get-RemoteAccessField $counts 'other')
        }
    }

    $uptime = ConvertTo-RemoteAccessCount (Get-RemoteAccessField $hostInfo 'uptime_seconds')
    $observedAt = ConvertTo-RemoteAccessTimestamp (Get-RemoteAccessField $Inventory 'observed_at_utc')
    if ($observedAt -eq 'unknown') { $observedAt = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ') }

    return [pscustomobject]@{
        observed_at_utc = $observedAt
        tailnet = [pscustomobject]@{
            state = $tailnetState
            unattended = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $tailnet 'unattended') @('enabled', 'disabled', 'unknown')
            last_state_change_utc = ConvertTo-RemoteAccessTimestamp (Get-RemoteAccessField $tailnet 'last_state_change_utc')
        }
        ssh = [pscustomobject]@{
            service = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $ssh 'service') @('running', 'stopped', 'missing', 'unknown')
            firewall = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $ssh 'firewall') @('tailnet-only', 'broad', 'unknown')
            last_listener_check_utc = ConvertTo-RemoteAccessTimestamp (Get-RemoteAccessField $ssh 'last_listener_check_utc')
        }
        desktop = [pscustomobject]@{
            task = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $desktop 'task') @('ready', 'running', 'absent', 'unknown')
            session = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $desktop 'session') @('interactive', 'signed-out', 'locked', 'unknown')
            rdc = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $desktop 'rdc') @('ready', 'unavailable', 'not-checked', 'unknown')
            denied_by_category = $denied
        }
        human_fallback = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $Inventory 'human_fallback') @('ready', 'disabled', 'unknown')
        host = [pscustomobject]@{
            uptime_seconds = $uptime
            last_boot_utc = ConvertTo-RemoteAccessTimestamp (Get-RemoteAccessField $hostInfo 'last_boot_utc')
        }
        last_audit = $lastAudit
    }
}

function Get-RemoteAccessTailscalePath {
    $command = Get-Command 'tailscale.exe' -ErrorAction SilentlyContinue
    if ($command) {
        if ($command.Path) { return $command.Path }
        if ($command.Source) { return $command.Source }
    }
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Tailscale\tailscale.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { return $candidate }
    }
    return $null
}

function Get-RemoteAccessTailscaleUnattendedState {
    try {
        $policy = Get-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Tailscale' -Name UnattendedMode -ErrorAction Stop
        switch ([string]$policy.UnattendedMode) {
            'always' { return 'enabled' }
            'never' { return 'disabled' }
            default { return 'unknown' }
        }
    } catch {
        return 'unknown'
    }
}

function ConvertTo-RemoteAccessDesktopSessionState {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$InteractiveSessionPresent,
        [AllowNull()][string]$InputDesktopName,
        [AllowNull()][object]$ProbeSucceeded
    )

    if ($ProbeSucceeded -ne $true) { return 'unknown' }
    if ($InteractiveSessionPresent -eq $false) { return 'signed-out' }
    if ($InteractiveSessionPresent -ne $true) { return 'unknown' }
    if ($InputDesktopName -eq 'Winlogon') { return 'locked' }
    if ($InputDesktopName -eq 'Default') { return 'interactive' }
    return 'unknown'
}

function Get-RemoteAccessFirewallState {
    [CmdletBinding()]
    param(
        [ValidateRange(1, 65535)][int]$Port = 22,
        [AllowNull()][string[]]$ExpectedProgramPaths = @()
    )

    try {
        $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
        $profileState = Get-RemoteAccessFirewallProfileState -Profiles $profiles
        if ($profileState -eq 'broad') { return 'broad' }
        if ($profileState -ne 'blocked') { return 'unknown' }

        $policy = New-Object -ComObject HNetCfg.FwPolicy2 -ErrorAction Stop
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop)
        $listenerPaths = [System.Collections.Generic.List[string]]::new()
        foreach ($expectedPath in $ExpectedProgramPaths) {
            if (-not [string]::IsNullOrWhiteSpace($expectedPath)) { $listenerPaths.Add($expectedPath) }
        }
        foreach ($listener in $listeners) {
            try {
                $process = Get-Process -Id ([int]$listener.OwningProcess) -ErrorAction Stop
                if (-not [string]::IsNullOrWhiteSpace($process.Path)) { $listenerPaths.Add($process.Path) }
            } catch { }
        }
        $normalizedRules = [System.Collections.Generic.List[object]]::new()
        foreach ($rule in $policy.Rules) {
            if (-not $rule.Enabled -or [int]$rule.Direction -ne 1 -or [int]$rule.Action -ne 1) { continue }
            $protocol = [int]$rule.Protocol
            if ($protocol -ne 6 -and $protocol -ne 256) { continue }
            $localPorts = @(ConvertFrom-RemoteAccessFirewallLocalPorts $rule.LocalPorts)
            $portMatches = $false
            foreach ($localPort in $localPorts) {
                if ($localPort -eq 'Any' -or $localPort -eq [string]$Port) { $portMatches = $true; break }
                if ($localPort -match '^(\d+)-(\d+)$' -and [int]$Matches[1] -le $Port -and [int]$Matches[2] -ge $Port) { $portMatches = $true; break }
            }
            if (-not $portMatches) { continue }

            $program = [string]$rule.ApplicationName
            $service = [string]$rule.ServiceName
            $programIsAny = [string]::IsNullOrWhiteSpace($program) -or $program -eq '*'
            $programMatchesListener = $false
            if (-not $programIsAny) {
                $expandedProgram = [Environment]::ExpandEnvironmentVariables($program)
                foreach ($listenerPath in $listenerPaths) {
                    try {
                        if ([IO.Path]::GetFullPath($expandedProgram).TrimEnd('\') -ieq [IO.Path]::GetFullPath($listenerPath).TrimEnd('\')) {
                            $programMatchesListener = $true
                            break
                        }
                    } catch { }
                }
            }
            if (-not $programIsAny -and -not $programMatchesListener -and $service -ne 'sshd') { continue }

            $remoteAddresses = @(([string]$rule.RemoteAddresses -split ',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
            if ($remoteAddresses -contains '*' -or $remoteAddresses -contains 'Any' -or $remoteAddresses.Count -eq 0) { $remoteAddresses = @('Any') }
            $interfaces = @($rule.Interfaces | ForEach-Object { [string]$_ } | Where-Object { $_ })
            $normalizedRules.Add(@{
                local_port = $localPorts
                protocol = $protocol.ToString()
                program = if ($programIsAny) { 'Any' } else { $program }
                service = $service
                remote_addresses = $remoteAddresses
                interface_aliases = $interfaces
            })
        }
        if ($normalizedRules.Count -eq 0) { return 'blocked' }
        return ConvertTo-RemoteAccessFirewallState -Rules $normalizedRules.ToArray() -DefaultInboundAction 'Block' -ListenerProgramPaths $listenerPaths.ToArray() -Port $Port
    } catch {
        return 'unknown'
    }
}

function Get-RemoteSshInstallPlan {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Preflight)

    $blockers = [System.Collections.Generic.List[string]]::new()
    $warnings = [System.Collections.Generic.List[string]]::new()
    $actions = [System.Collections.Generic.List[string]]::new()

    if ((Get-RemoteAccessField $Preflight 'account_is_standard') -ne $true) { $blockers.Add('ssh-account-not-approved') }
    if ((Get-RemoteAccessField $Preflight 'public_key_valid') -ne $true) { $blockers.Add('public-key-invalid-or-missing') }
    if ((Get-RemoteAccessField $Preflight 'manifest_scope_matches') -eq $false) { $blockers.Add('existing-ssh-manifest-scope-changed') }
    if ((Get-RemoteAccessField $Preflight 'host_key_path_safe') -ne $true) { $blockers.Add('ssh-host-key-path-unsafe') }

    $addresses = @(Get-RemoteAccessField $Preflight 'trusted_client_addresses' | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
    if ($addresses.Count -eq 0) {
        $blockers.Add('trusted-client-address-required')
    } else {
        foreach ($address in $addresses) {
            if (-not (Test-RemoteSshClientAddress ([string]$address))) {
                $blockers.Add('trusted-client-address-invalid')
                break
            }
        }
    }

    $port = 22
    try { $port = [int](Get-RemoteAccessField $Preflight 'port') } catch { $blockers.Add('selected-port-invalid') }
    if ($port -lt 1 -or $port -gt 65535) { $blockers.Add('selected-port-invalid') }
    if ($port -ne 22 -and (Get-RemoteAccessField $Preflight 'alternate_port_approved') -ne $true) { $blockers.Add('alternate-port-approval-required') }
    if ((Get-RemoteAccessField $Preflight 'port_in_use') -eq $true) { $blockers.Add('selected-port-in-use') }
    if ((Get-RemoteAccessField $Preflight 'port_in_use') -ne $true -and (Get-RemoteAccessField $Preflight 'port_in_use') -ne $false) { $blockers.Add('selected-port-state-unknown') }
    if ((Get-RemoteAccessField $Preflight 'custom_sshd_config') -eq $true) { $blockers.Add('existing-sshd-config-conflict') }

    $defaultRuleState = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $Preflight 'default_openssh_rule_state') @('absent', 'preexisting-broad', 'feature-managed-broad', 'scoped')
    if ($defaultRuleState -eq 'preexisting-broad') { $blockers.Add('preexisting-broad-openssh-rule') }
    if ($defaultRuleState -eq 'unknown') { $blockers.Add('default-openssh-rule-state-unknown') }

    if ((Get-RemoteAccessField $Preflight 'authorized_key_exists') -eq $true) {
        $actions.Add('keep-authorized-key')
    } else {
        $actions.Add('add-authorized-key')
    }

    $serviceState = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $Preflight 'service_state') @('running', 'stopped', 'missing', 'unknown')
    if ($serviceState -eq 'running') {
        $actions.Add('keep-running-service')
    } elseif ($serviceState -eq 'missing' -or $serviceState -eq 'stopped') {
        $actions.Add('configure-ssh-service')
    } else {
        $blockers.Add('ssh-service-state-unknown')
    }

    $firewallState = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $Preflight 'firewall_state') @('tailnet-only', 'blocked', 'broad', 'unknown')
    $firewallDefaultAllow = Get-RemoteAccessField $Preflight 'firewall_default_allow'
    if ($firewallDefaultAllow -eq $true) { $blockers.Add('inbound-firewall-default-allows') }
    elseif ($firewallDefaultAllow -ne $false) { $blockers.Add('inbound-firewall-default-state-unknown') }
    $finalize = (Get-RemoteAccessField $Preflight 'finalize_firewall') -eq $true
    if ($firewallState -eq 'broad') {
        if (-not $finalize) {
            $warnings.Add('broad-firewall-rule-preserved-pending-verification')
            if ($defaultRuleState -ne 'feature-managed-broad') { $blockers.Add('unowned-broad-firewall-rule') }
        } elseif ((Get-RemoteAccessField $Preflight 'second_device_verified') -ne $true) {
            $blockers.Add('second-device-verification-required')
        } elseif ($defaultRuleState -ne 'feature-managed-broad' -or (Get-RemoteAccessField $Preflight 'broad_rule_manifested') -ne $true) {
            $blockers.Add('broad-rule-not-feature-manifested')
        } else {
            $actions.Add('disable-broad-default-rule')
        }
    } elseif ($firewallState -eq 'unknown') {
        $blockers.Add('firewall-state-unknown')
    } elseif ($firewallState -eq 'blocked') {
        $actions.Add('create-scoped-firewall-rule')
    } else {
        $actions.Add('keep-scoped-firewall-rule')
    }

    return [pscustomobject]@{
        ready = ($blockers.Count -eq 0)
        port = $port
        blockers = @($blockers.ToArray())
        warnings = @($warnings.ToArray())
        actions = @($actions.ToArray())
    }
}

function Test-RemoteAccessResourceOwned {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$Manifest,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $resources = Get-RemoteAccessField $Manifest 'resources'
    if ($null -eq $resources) {
        $components = Get-RemoteAccessField $Manifest 'components'
        $ssh = Get-RemoteAccessField $components 'ssh'
        $resources = Get-RemoteAccessField $ssh 'resources'
    }
    $resource = Get-RemoteAccessField $resources $Name
    return (Get-RemoteAccessField $resource 'managed') -eq $true
}

function Get-RemoteAccessRollbackPlan {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$Manifest,
        [ValidateSet('Ssh')][string]$Component = 'Ssh'
    )

    $names = @('ssh_firewall_v4', 'ssh_firewall_v6', 'default_openssh_rule', 'authorized_keys', 'sshd_config', 'ssh_service', 'ssh_capability')
    $managed = New-Object 'System.Collections.Generic.List[string]'
    foreach ($name in $names) {
        if (Test-RemoteAccessResourceOwned -Manifest $Manifest -Name $name) { $managed.Add($name) }
    }
    return [pscustomobject]@{
        component = $Component.ToLowerInvariant()
        ready = ($managed.Count -gt 0)
        managed_resources = @($managed.ToArray())
    }
}

function Get-RemoteAccessDesktopSessionState {
    $queryUser = Join-Path $env:SystemRoot 'System32\quser.exe'
    if (-not (Test-Path -LiteralPath $queryUser)) { return 'unknown' }
    try {
        $lines = @(& $queryUser 2>$null)
        if ($LASTEXITCODE -ne 0) { return 'unknown' }
        $sessions = @($lines | Select-Object -Skip 1 | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        if ($sessions.Count -eq 0) {
            return ConvertTo-RemoteAccessDesktopSessionState -InteractiveSessionPresent $false -InputDesktopName $null -ProbeSucceeded $true
        }

        $processSession = (Get-Process -Id $PID -ErrorAction Stop).SessionId
        $matchingActiveSession = $false
        foreach ($line in $sessions) {
            if ($line -match '\s(?<sessionId>\d+)\s+(?<state>Active|Disc)\s') {
                if ($Matches.state -eq 'Active' -and [int]$Matches.sessionId -eq [int]$processSession) {
                    $matchingActiveSession = $true
                    break
                }
            }
        }
        if (-not $matchingActiveSession -or [int]$processSession -eq 0) { return 'unknown' }

        if (-not ([System.Management.Automation.PSTypeName]'RemoteAccessDesktopNative').Type) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class RemoteAccessDesktopNative {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint desiredAccess);
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool GetUserObjectInformation(IntPtr handle, int index, StringBuilder name, int length, out int needed);
}
'@ -ErrorAction Stop | Out-Null
        }

        $desktop = [RemoteAccessDesktopNative]::OpenInputDesktop(0, $false, 1)
        if ($desktop -eq [IntPtr]::Zero) { return 'unknown' }
        try {
            $name = New-Object System.Text.StringBuilder 128
            $needed = 0
            $probeSucceeded = [RemoteAccessDesktopNative]::GetUserObjectInformation($desktop, 2, $name, $name.Capacity, [ref]$needed)
            if (-not $probeSucceeded) { return 'unknown' }
            return ConvertTo-RemoteAccessDesktopSessionState -InteractiveSessionPresent $true -InputDesktopName $name.ToString() -ProbeSucceeded $true
        } finally {
            [void][RemoteAccessDesktopNative]::CloseDesktop($desktop)
        }
    } catch {
        return 'unknown'
    }
}

function Get-RemoteAccessInventory {
    $now = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    $tailnet = @{
        state = 'unknown'
        unattended = Get-RemoteAccessTailscaleUnattendedState
        interface_present = $false
        address_present = $false
        last_state_change_utc = $null
    }

    $tailscaleAdapters = @()
    try {
        $tailscaleAdapters = @(Get-NetAdapter -ErrorAction Stop | Where-Object {
            ($_.Name -match '(?i)tailscale') -or ($_.InterfaceDescription -match '(?i)tailscale')
        } | Where-Object Status -eq 'Up')
        $tailnet.interface_present = $tailscaleAdapters.Count -gt 0
    } catch { }

    $tailscalePath = Get-RemoteAccessTailscalePath
    if ($tailscalePath) {
        try {
            $rawStatus = @(& $tailscalePath status --json 2>$null)
            if ($LASTEXITCODE -eq 0) {
                $ts = ($rawStatus -join "`n") | ConvertFrom-Json -ErrorAction Stop
                $addresses = @($ts.Self.TailscaleIPs | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
                $tailnet.address_present = $addresses.Count -gt 0
                if ($tailnet.interface_present -and $tailnet.address_present) {
                    if ($ts.BackendState -eq 'Running' -and $ts.Self.Online -eq $true) {
                        $tailnet.state = 'connected'
                    } elseif ($ts.BackendState -eq 'Stopped' -or $ts.Self.Online -eq $false) {
                        $tailnet.state = 'disconnected'
                    }
                }
            }
        } catch { }
    }

    $sshService = 'missing'
    try {
        $service = Get-Service -Name 'sshd' -ErrorAction Stop
        if ($service.Status -eq 'Running') { $sshService = 'running' } else { $sshService = 'stopped' }
    } catch {
        if (Get-Command Get-Service -ErrorAction SilentlyContinue) { $sshService = 'missing' }
    }
    $sshPort = Get-RemoteAccessSshPort
    $listenerCheck = 'unknown'
    $sshListenerState = 'unknown'
    if ($null -ne $sshPort) {
        try {
            $listeners = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$sshPort) -ErrorAction Stop)
            $listenerCheck = $now
            $normalizedListeners = @()
            $sshdProcessIds = New-Object 'System.Collections.Generic.List[int]'
            foreach ($listener in $listeners) {
                $normalizedListeners += @{ local_address = [string]$listener.LocalAddress; owning_process = [int]$listener.OwningProcess }
                try {
                    $process = Get-Process -Id ([int]$listener.OwningProcess) -ErrorAction Stop
                    if ($process.ProcessName -ieq 'sshd') { $sshdProcessIds.Add([int]$listener.OwningProcess) }
                } catch { }
            }
            $tailnetLocalAddresses = @()
            foreach ($adapter in $tailscaleAdapters) {
                try {
                    $tailnetLocalAddresses += @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -ErrorAction Stop | ForEach-Object { [string]$_.IPAddress } | Where-Object { Test-RemoteAccessTailnetAddress $_ })
                } catch { }
            }
            if (Test-RemoteAccessSshListener -Listeners $normalizedListeners -SshdProcessIds $sshdProcessIds.ToArray() -TailscaleAddresses $tailnetLocalAddresses) {
                $sshListenerState = 'running'
            } else {
                $sshListenerState = 'stopped'
            }
        } catch { }
    }
    if ($sshService -eq 'running' -and $sshListenerState -ne 'running') {
        if ($sshListenerState -eq 'stopped') { $sshService = 'stopped' } else { $sshService = 'unknown' }
    }
    $sshFirewall = 'unknown'
    if ($null -ne $sshPort) {
        $sshFirewall = Get-RemoteAccessFirewallState -Port ([int]$sshPort) -ExpectedProgramPaths @((Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'))
    }

    $sessionState = Get-RemoteAccessDesktopSessionState
    $rdcPath = Join-Path $env:LOCALAPPDATA 'Programs\rdc\rdc.exe'
    $currentSid = $null
    $currentSessionId = 0
    try {
        $currentSid = [string]([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
        $currentSessionId = [int](Get-Process -Id $PID -ErrorAction Stop).SessionId
    } catch { }
    $rdcTaskName = 'dev.rdc.daemon.' + $env:USERNAME
    $rdcTaskState = Get-RemoteAccessRdcTaskState -TaskName $rdcTaskName -UserSid ([string]$currentSid) -RdcPath $rdcPath
    $desktopTask = $rdcTaskState.state
    $rdcStatus = 'unavailable'
    $rdcManifestPath = Join-Path (Split-Path -Parent $rdcPath) 'ai-stack-manifest.json'
    if ($rdcTaskState.valid -and $rdcTaskState.running -and (Test-Path -LiteralPath $rdcPath -PathType Leaf) -and (Test-Path -LiteralPath $rdcManifestPath -PathType Leaf)) {
        try {
            $rdcManifest = Get-Content -LiteralPath $rdcManifestPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
            $rdcPort = [int]$rdcManifest.port
            $rdcAddress = [string]$rdcManifest.bind_address
            $binaryHash = (Get-FileHash -LiteralPath $rdcPath -Algorithm SHA256).Hash
            $rdcConfigPath = Join-Path (Join-Path $env:APPDATA 'rdc') 'config.toml'
            $configHash = (Get-FileHash -LiteralPath $rdcConfigPath -Algorithm SHA256 -ErrorAction Stop).Hash
            $rdcFirewall = Get-RemoteAccessFirewallState -Port $rdcPort -ExpectedProgramPaths @($rdcPath)
            $manifestValid = ($rdcManifest.schema_version -eq 1 -and $rdcManifest.component -eq 'rdc' -and $rdcManifest.state -eq 'active' -and [string]$rdcManifest.account.sid -eq $currentSid -and [string]$rdcManifest.resources.binary.sha256 -eq $binaryHash -and [string]$rdcManifest.resources.config.sha256 -eq $configHash -and $rdcFirewall -eq 'tailnet-only' -and (Test-RemoteAccessTailnetAddress $rdcAddress))
            if ($manifestValid -and $sessionState -eq 'interactive' -and $currentSessionId -gt 0) {
                $rdcProcesses = @(Get-Process -Name 'rdc' -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -eq $currentSessionId -and $_.Path -and ([IO.Path]::GetFullPath($_.Path) -ieq [IO.Path]::GetFullPath($rdcPath)) })
                $rdcListeners = @(Get-NetTCPConnection -State Listen -LocalPort $rdcPort -ErrorAction SilentlyContinue | ForEach-Object { @{ local_address = [string]$_.LocalAddress; owning_process = [int]$_.OwningProcess } })
                $rdcProcessIds = @($rdcProcesses | ForEach-Object { [int]$_.Id })
                if (Test-RemoteAccessRdcListener -Listeners $rdcListeners -RdcProcessIds $rdcProcessIds -TailnetAddresses @($rdcAddress)) { $rdcStatus = 'ready' }
            }
        } catch { }
    }

    $lastBoot = 'unknown'
    $uptimeSeconds = $null
    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
        $boot = [DateTime]$os.LastBootUpTime
        $lastBoot = $boot.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        $uptimeSeconds = [long][Math]::Max(0, ((Get-Date) - $boot).TotalSeconds)
    } catch { }

    return @{
        observed_at_utc = $now
        tailnet = $tailnet
        ssh = @{
            service = $sshService
            firewall = $sshFirewall
            last_listener_check_utc = $listenerCheck
        }
        desktop = @{
            task = $desktopTask
            session = $sessionState
            rdc = $rdcStatus
            denied_by_category = $null
            audit = @{ valid = $false }
        }
        human_fallback = Get-RemoteAccessHumanFallbackState
        host = @{ uptime_seconds = $uptimeSeconds; last_boot_utc = $lastBoot }
    }
}

function Get-RemoteAccessStatus {
    [CmdletBinding()]
    param()

    try {
        return ConvertTo-RemoteAccessStatus (Get-RemoteAccessInventory)
    } catch {
        return ConvertTo-RemoteAccessStatus $null
    }
}

function Test-RemoteAccessRdcIdentity {
    [CmdletBinding()]
    param([AllowNull()][string]$Identity)

    if ([string]::IsNullOrWhiteSpace($Identity)) { return $false }
    $value = $Identity.Trim()
    if ($value -cne $Identity -or $value.Length -gt 254 -or $value -match '[*?\s\x00-\x1f\x7f]' -or $value -match '["''`,;=\\/\[\]{}]') { return $false }
    if ($value -match '^(?i:autogroup):') { return $false }
    if ($value -match '^(?i:tag):[A-Za-z0-9_-]{1,63}$') { return $true }
    if ($value -match '^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$') { return $true }
    if ($value -match '^[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9][A-Za-z0-9.-]{0,190}$') { return $true }
    return $false
}

function ConvertTo-RemoteAccessRdcIdentityList {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string[]]$InputIdentity,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $normalized = New-Object 'System.Collections.Generic.List[string]'
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($identity in $InputIdentity) {
        if (-not (Test-RemoteAccessRdcIdentity -Identity $identity)) { throw "The $Name identity list contains an empty, broad, or unsupported value." }
        if (-not $seen.Add($identity)) { throw "The $Name identity list contains a duplicate value." }
        $normalized.Add($identity)
    }
    return @($normalized.ToArray() | Sort-Object { $_.ToLowerInvariant() })
}

function ConvertTo-RemoteAccessTomlString {
    param([Parameter(Mandatory = $true)][string]$Value)
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

function New-RemoteAccessRdcConfig {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][ValidateRange(1, 65535)][int]$Port,
        [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$BindAddress,
        [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string[]]$AllowedIdentities,
        [string[]]$ViewOnlyIdentities = @()
    )

    if (-not (Test-RemoteAccessTailnetAddress -Address $BindAddress)) { throw 'The rdc bind address must be a verified Tailscale address.' }
    $inputIdentities = @(ConvertTo-RemoteAccessRdcIdentityList -InputIdentity $AllowedIdentities -Name 'input-capable')
    $viewIdentities = @()
    if ($ViewOnlyIdentities.Count -gt 0) {
        $viewIdentities = @(ConvertTo-RemoteAccessRdcIdentityList -InputIdentity $ViewOnlyIdentities -Name 'view-only')
    }
    $inputSet = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($identity in $inputIdentities) { [void]$inputSet.Add($identity) }
    foreach ($identity in $viewIdentities) {
        if ($inputSet.Contains($identity)) { throw 'An identity cannot receive both input and view-only grants.' }
    }

    $entries = New-Object 'System.Collections.Generic.List[string]'
    foreach ($identity in $inputIdentities) {
        $who = ConvertTo-RemoteAccessTomlString -Value $identity
        $entries.Add("  { who = $who, can = [`"view`", `"input`"] }")
    }
    foreach ($identity in $viewIdentities) {
        $who = ConvertTo-RemoteAccessTomlString -Value $identity
        $entries.Add("  { who = $who, can = [`"view`"] }")
    }
    $allowLines = @($entries.ToArray()) -join ",`r`n"
    $bind = ConvertTo-RemoteAccessTomlString -Value $BindAddress
    return @"
[serve]
port = $Port
bind = $bind
allow = [
$allowLines
]

[serve.audit]
enabled = true
max_size_mb = 50
keep = 5
"@
}

function Test-RemoteAccessRdcArchiveHash {
    [CmdletBinding()]
    param([AllowNull()][string]$ActualHash, [AllowNull()][string]$ExpectedHash)

    if ($ActualHash -notmatch '^[A-Fa-f0-9]{64}$' -or $ExpectedHash -notmatch '^[A-Fa-f0-9]{64}$') { return $false }
    return $ActualHash.Equals($ExpectedHash, [StringComparison]::OrdinalIgnoreCase)
}

function Test-RemoteAccessRdcConfigAcl {
    [CmdletBinding()]
    param(
        [bool]$IsProtected,
        [AllowNull()][string[]]$WriteSids,
        [Parameter(Mandatory = $true)][string]$OwnerSid,
        [Parameter(Mandatory = $true)][string[]]$AllowedSids
    )

    if (-not $IsProtected -or $null -eq $WriteSids) { return $false }
    $allowed = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($sid in $AllowedSids) { [void]$allowed.Add([string]$sid) }
    if ([string]::IsNullOrWhiteSpace($OwnerSid) -or -not $allowed.Contains($OwnerSid)) { return $false }
    foreach ($sid in $WriteSids) {
        if (-not $allowed.Contains([string]$sid)) { return $false }
    }
    return $true
}

function Test-RemoteAccessRdcTask {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Task)

    $taskName = [string](Get-RemoteAccessField $Task 'name')
    $expectedName = [string](Get-RemoteAccessField $Task 'expected_name')
    $taskPath = [string](Get-RemoteAccessField $Task 'path')
    $expectedTaskPath = [string](Get-RemoteAccessField $Task 'expected_task_path')
    $actionCount = [int](Get-RemoteAccessField $Task 'action_count')
    $state = [string](Get-RemoteAccessField $Task 'state')
    $userSid = [string](Get-RemoteAccessField $Task 'user_sid')
    $expectedUserSid = [string](Get-RemoteAccessField $Task 'expected_user_sid')
    $runLevel = [string](Get-RemoteAccessField $Task 'run_level')
    $logonType = [string](Get-RemoteAccessField $Task 'logon_type')
    $executable = [string](Get-RemoteAccessField $Task 'executable')
    $expectedExecutable = [string](Get-RemoteAccessField $Task 'expected_executable')
    $arguments = [string](Get-RemoteAccessField $Task 'arguments')

    if ([string]::IsNullOrWhiteSpace($taskName) -or $taskName -ine $expectedName) { return $false }
    if ([string]::IsNullOrWhiteSpace($taskPath)) { return $false }
    if ($actionCount -ne 1) { return $false }
    if ($expectedTaskPath -and $taskPath -ine $expectedTaskPath) { return $false }
    if ($state -notin @('Running', 'Ready')) { return $false }
    if ([string]::IsNullOrWhiteSpace($userSid) -or $userSid -ine $expectedUserSid) { return $false }
    if ($runLevel -notin @('Limited', 'LeastPrivilege')) { return $false }
    if ($logonType -notin @('Interactive', 'InteractiveToken')) { return $false }
    try {
        if ([IO.Path]::GetFullPath($executable) -ine [IO.Path]::GetFullPath($expectedExecutable)) { return $false }
    } catch { return $false }
    if ($arguments -notmatch '(?i)^\s*serve(?:\s|$)' -or $arguments -match '(?i)(?:^|\s)--(?:allow|bind|clipboard|dev-loopback|elevated|port)(?:=|\s|$)') { return $false }
    return $true
}

function ConvertTo-RemoteAccessAccountSid {
    [CmdletBinding()]
    param([AllowNull()][string]$Name)
    if ([string]::IsNullOrWhiteSpace($Name)) { return $null }
    try {
        if ($Name -match '^S-1-') { return ([Security.Principal.SecurityIdentifier]$Name).Value }
        return ([Security.Principal.NTAccount]$Name).Translate([Security.Principal.SecurityIdentifier]).Value
    } catch { return $null }
}

function Get-RemoteAccessRdcTaskState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$UserSid,
        [Parameter(Mandatory = $true)][string]$RdcPath
    )

    if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) {
        return [pscustomobject]@{ state = 'unknown'; present = $false; valid = $false; running = $false }
    }
    try {
        $tasks = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { [string]$_.TaskName -ieq $TaskName })
        if (-not $tasks) { return [pscustomobject]@{ state = 'absent'; present = $false; valid = $false; running = $false } }
        if ($tasks.Count -ne 1) { return [pscustomobject]@{ state = 'unknown'; present = $true; valid = $false; running = $false } }
        $task = $tasks[0]
        $manifestPath = Join-Path (Split-Path -Parent $RdcPath) 'ai-stack-manifest.json'
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return [pscustomobject]@{ state = 'unknown'; present = $true; valid = $false; running = $false } }
        $rdcManifest = Get-Content -LiteralPath $manifestPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        if ($rdcManifest.schema_version -ne 1 -or $rdcManifest.component -ne 'rdc' -or $rdcManifest.state -ne 'active' -or [string]$rdcManifest.account.sid -ne $UserSid -or [string]::IsNullOrWhiteSpace([string]$rdcManifest.resources.task.task_path)) {
            return [pscustomobject]@{ state = 'unknown'; present = $true; valid = $false; running = $false }
        }
        $actions = @($task.Actions)
        if ($actions.Count -ne 1) { return [pscustomobject]@{ state = 'unknown'; present = $true; valid = $false; running = $false } }
        $action = $actions[0]
        $snapshot = @{
            name = [string]$task.TaskName
            expected_name = $TaskName
            path = [string]$task.TaskPath
            expected_task_path = [string]$rdcManifest.resources.task.task_path
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
        $valid = Test-RemoteAccessRdcTask -Task $snapshot
        if (-not $valid) { return [pscustomobject]@{ state = 'unknown'; present = $true; valid = $false; running = $false } }
        return [pscustomobject]@{ state = [string]$task.State.ToLowerInvariant(); present = $true; valid = $true; running = ([string]$task.State -eq 'Running') }
    } catch {
        return [pscustomobject]@{ state = 'unknown'; present = $false; valid = $false; running = $false }
    }
}

function Test-RemoteAccessRdcListener {
    [CmdletBinding()]
    param(
        [AllowNull()][object[]]$Listeners,
        [AllowNull()][int[]]$RdcProcessIds,
        [AllowNull()][string[]]$TailnetAddresses
    )

    if (-not $Listeners -or -not $RdcProcessIds -or -not $TailnetAddresses) { return $false }
    $rdcPidSet = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($processId in $RdcProcessIds) { [void]$rdcPidSet.Add([int]$processId) }
    $tailnetAddressSet = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($address in $TailnetAddresses) {
        if (Test-RemoteAccessTailnetAddress $address) { [void]$tailnetAddressSet.Add((ConvertTo-RemoteAccessAddressKey $address)) }
    }
    if ($tailnetAddressSet.Count -eq 0) { return $false }
    foreach ($listener in $Listeners) {
        $processId = Get-RemoteAccessField $listener 'owning_process'
        $localAddress = [string](Get-RemoteAccessField $listener 'local_address')
        if ($null -eq $processId -or -not $rdcPidSet.Contains([int]$processId)) { return $false }
        if (-not $tailnetAddressSet.Contains((ConvertTo-RemoteAccessAddressKey $localAddress))) { return $false }
    }
    return $true
}

function Test-RemoteAccessRdcInstallTaskState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][ValidateSet('absent', 'active-identical', 'conflict')][string]$InstallState,
        [bool]$TaskPresent,
        [bool]$TaskValid
    )

    if ($InstallState -eq 'conflict') { return $false }
    if ($InstallState -eq 'absent' -and $TaskPresent) { return $false }
    if ($InstallState -eq 'active-identical' -and $TaskPresent -and -not $TaskValid) { return $false }
    return $true
}

function Set-RemoteAccessRdcTaskPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Manifest,
        [Parameter(Mandatory = $true)][object]$TaskFacts
    )

    if ((Get-RemoteAccessField $TaskFacts 'valid') -ne $true) { return $false }
    $task = Get-RemoteAccessField $TaskFacts 'task'
    $taskPath = [string](Get-RemoteAccessField $task 'TaskPath')
    if ([string]::IsNullOrWhiteSpace($taskPath) -or $taskPath -notmatch '^\\(?:[^\\]+\\)*$') { return $false }
    $resources = Get-RemoteAccessField $Manifest 'resources'
    $taskResource = Get-RemoteAccessField $resources 'task'
    if ($null -eq $taskResource -or (Get-RemoteAccessField $taskResource 'managed') -ne $true) { return $false }
    Set-RemoteAccessManifestProperty -Manifest $taskResource -Name 'task_path' -Value $taskPath
    return $true
}

function Test-RemoteAccessRdcRollbackTaskState {
    [CmdletBinding()]
    param(
        [bool]$TaskPresent,
        [bool]$TaskMatchesManifest,
        [bool]$TaskManaged,
        [AllowNull()][string]$ExpectedTaskPath,
        [AllowNull()][string]$ActualTaskPath
    )

    if (-not $TaskPresent) { return $true }
    if (-not $TaskManaged -or -not $TaskMatchesManifest) { return $false }
    if ([string]::IsNullOrWhiteSpace($ExpectedTaskPath) -or [string]::IsNullOrWhiteSpace($ActualTaskPath)) { return $false }
    return ($ActualTaskPath -ieq $ExpectedTaskPath)
}

function Get-RemoteAccessRdcInstallPlan {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][System.Collections.IDictionary]$Preflight)

    $blockers = New-Object 'System.Collections.Generic.List[string]'
    $actions = New-Object 'System.Collections.Generic.List[string]'
    if ($Preflight.identities_valid -ne $true) { $blockers.Add('verified-identities-required') }
    if ($Preflight.archive_hash_valid -ne $true) { $blockers.Add('pinned-archive-hash-mismatch') }
    if ($Preflight.config_acl_valid -ne $true) { $blockers.Add('rdc-config-acl-untrusted') }
    if ($Preflight.user_paths_safe -ne $true) { $blockers.Add('user-profile-path-untrusted') }
    if ([string]$Preflight.session_state -ne 'interactive' -or [int]$Preflight.current_session_id -le 0) { $blockers.Add('interactive-session-required') }
    if ($Preflight.bind_address_valid -ne $true) { $blockers.Add('verified-tailnet-bind-address-required') }
    if ($Preflight.port_available -ne $true) { $blockers.Add('rdc-port-unavailable') }
    if ([string]$Preflight.ssh_manifest_active -ne 'active' -or $Preflight.ssh_acceptance_attested -ne $true -or $Preflight.boot_recovery_ready -ne $true) {
        $blockers.Add('ssh-recovery-acceptance-required')
    }
    if ([string]$Preflight.firewall_state -eq 'broad' -or $Preflight.firewall_default_allow -eq $true) { $blockers.Add('rdc-firewall-already-broad') }
    if ([string]$Preflight.firewall_state -eq 'unknown') { $blockers.Add('firewall-state-unknown') }
    if ($Preflight.firewall_default_allow -ne $false) { $blockers.Add('firewall-default-policy-unknown') }
    if ($Preflight.firewall_rule_present -ne $true -and $Preflight.firewall_admin_context -ne $true) { $blockers.Add('administrator-required-for-tailnet-firewall') }
    if ([string]$Preflight.install_state -eq 'conflict') { $blockers.Add('unmanaged-rdc-state-conflict') }

    if ($blockers.Count -eq 0) {
        if ([string]$Preflight.install_state -eq 'absent') { $actions.Add('install-pinned-binary-and-config') }
        if ($Preflight.firewall_rule_present -ne $true) { $actions.Add('create-tailnet-scoped-firewall-rule') }
        if ($Preflight.task_valid -ne $true) { $actions.Add('register-standard-user-logon-task') }
        elseif ($Preflight.task_running -ne $true) { $actions.Add('start-standard-user-logon-task') }
        if ($actions.Count -eq 0) { $actions.Add('keep-healthy-managed-installation') }
    }
    return [pscustomobject]@{
        ready = ($blockers.Count -eq 0)
        blockers = @($blockers.ToArray())
        actions = @($actions.ToArray())
    }
}

function Invoke-RemoteAccessRdcMutation {
    [CmdletBinding()]
    param([switch]$Apply, [Parameter(Mandatory = $true)][scriptblock]$Mutation)

    if (-not $Apply) { return $false }
    & $Mutation
    return $true
}

function Get-RemoteAccessRdcRollbackPlan {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$Manifest,
        [AllowNull()][string]$CurrentBinaryHash,
        [AllowNull()][string]$CurrentConfigHash,
        [bool]$TaskMatchesManifest,
        [bool]$FirewallMatchesManifest,
        [bool]$TaskPresent = $false,
        [bool]$ProcessRunning = $false,
        [bool]$ProcessStateKnown = $true
    )

    $blockers = New-Object 'System.Collections.Generic.List[string]'
    $resources = Get-RemoteAccessField $Manifest 'resources'
    if ($null -eq $Manifest -or (Get-RemoteAccessField $Manifest 'schema_version') -ne 1 -or (Get-RemoteAccessField $Manifest 'component') -ne 'rdc') {
        $blockers.Add('rdc-manifest-invalid')
    } elseif ((Get-RemoteAccessField $Manifest 'state') -notin @('active', 'applying', 'rolling_back')) {
        $blockers.Add('rdc-manifest-not-active')
    }
    foreach ($item in @(
        @{ name = 'binary'; hash = $CurrentBinaryHash },
        @{ name = 'config'; hash = $CurrentConfigHash }
    )) {
        $resource = Get-RemoteAccessField $resources $item.name
        if ((Get-RemoteAccessField $resource 'managed') -eq $true -and $item.hash -and [string]$item.hash -cne [string](Get-RemoteAccessField $resource 'sha256')) {
            $blockers.Add("rdc-$($item.name)-changed")
        }
    }
    if (-not $TaskMatchesManifest) { $blockers.Add('rdc-task-scope-changed') }
    if (-not $FirewallMatchesManifest) { $blockers.Add('rdc-firewall-scope-changed') }
    if (-not $ProcessStateKnown) { $blockers.Add('rdc-process-state-unknown') }
    elseif ($ProcessRunning -and -not $TaskPresent) { $blockers.Add('rdc-orphan-process-running') }
    $managed = New-Object 'System.Collections.Generic.List[string]'
    foreach ($name in @('task', 'firewall', 'config', 'binary')) {
        if ((Get-RemoteAccessField (Get-RemoteAccessField $resources $name) 'managed') -eq $true) { $managed.Add($name) }
    }
    return [pscustomobject]@{
        ready = ($blockers.Count -eq 0 -and $managed.Count -gt 0)
        blockers = @($blockers.ToArray())
        managed_resources = @($managed.ToArray())
    }
}
