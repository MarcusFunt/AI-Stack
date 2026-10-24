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
AllowUsers $WindowsAccount
AuthorizedKeysFile .ssh/authorized_keys
Subsystem sftp sftp-server.exe
"@
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

function ConvertTo-RemoteAccessFirewallState {
    [CmdletBinding()]
    param(
        [AllowNull()][object[]]$Rules,
        [AllowNull()][object]$DefaultInboundAction,
        [AllowNull()][string[]]$ListenerProgramPaths,
        [ValidateRange(1, 65535)][int]$Port = 22
    )

    if ([string]$DefaultInboundAction -eq 'Allow' -or [string]$DefaultInboundAction -eq '6') { return 'broad' }
    if ($null -eq $Rules) { return 'unknown' }
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
        $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop | Where-Object { $_.Enabled })
        if (-not $profiles) { return 'unknown' }
        $defaultInboundActions = @($profiles | ForEach-Object { $_.DefaultInboundAction.ToString() } | Sort-Object -Unique)
        if ($defaultInboundActions -contains 'Allow') { return 'broad' }
        if (@($defaultInboundActions | Where-Object { $_ -ne 'Block' }).Count -gt 0) { return 'unknown' }

        $policy = New-Object -ComObject HNetCfg.FwPolicy2 -ErrorAction Stop
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
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
            $localPorts = @(([string]$rule.LocalPorts -split ',') | ForEach-Object { $_.Trim() })
            if ($localPorts.Count -eq 0) { $localPorts = @('Any') }
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

    $firewallState = ConvertTo-RemoteAccessEnum (Get-RemoteAccessField $Preflight 'firewall_state') @('tailnet-only', 'broad', 'unknown')
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
        if ($finalize) { $blockers.Add('firewall-state-unknown') }
        else { $actions.Add('create-scoped-firewall-rule') }
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

    $desktopTask = 'absent'
    try {
        $tasks = @(Get-ScheduledTask -TaskPath '\AI-Stack\RemoteAccess\' -ErrorAction Stop)
        $rdcTask = $tasks | Where-Object TaskName -eq 'RdcDesktop' | Select-Object -First 1
        if ($rdcTask) {
            if ($rdcTask.State -eq 'Running') { $desktopTask = 'running' }
            elseif ($rdcTask.State -eq 'Ready') { $desktopTask = 'ready' }
            else { $desktopTask = 'unknown' }
        }
    } catch { }

    $sessionState = Get-RemoteAccessDesktopSessionState
    $rdcPath = Join-Path $env:LOCALAPPDATA 'Programs\rdc\rdc.exe'
    $rdcStatus = 'unavailable'
    if (Test-Path -LiteralPath $rdcPath) {
        try {
            $rdcProcesses = @(Get-Process -Name 'rdc' -ErrorAction SilentlyContinue | Where-Object { $_.SessionId -gt 0 })
            if ($rdcProcesses.Count -gt 0 -and $sessionState -eq 'interactive') { $rdcStatus = 'ready' }
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
        human_fallback = 'unknown'
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
