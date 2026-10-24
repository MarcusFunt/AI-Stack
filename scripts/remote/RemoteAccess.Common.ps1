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

function ConvertTo-RemoteAccessFirewallState {
    [CmdletBinding()]
    param(
        [AllowNull()][object[]]$Rules,
        [AllowNull()][object]$DefaultInboundAction,
        [AllowNull()][string[]]$ListenerProgramPaths
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

        $isPort22 = $localPorts -contains '22'
        $isAnyPort = $localPorts -contains 'Any'
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
    try {
        $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop | Where-Object { $_.Enabled })
        if (-not $profiles) { return 'unknown' }
        $defaultInboundActions = @($profiles | ForEach-Object { $_.DefaultInboundAction.ToString() } | Sort-Object -Unique)
        if ($defaultInboundActions -contains 'Allow') { return 'broad' }
        if (@($defaultInboundActions | Where-Object { $_ -ne 'Block' }).Count -gt 0) { return 'unknown' }

        $policy = New-Object -ComObject HNetCfg.FwPolicy2 -ErrorAction Stop
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 22 -ErrorAction SilentlyContinue)
        $listenerPaths = [System.Collections.Generic.List[string]]::new()
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
                if ($localPort -eq 'Any' -or $localPort -eq '22') { $portMatches = $true; break }
                if ($localPort -match '^(\d+)-(\d+)$' -and [int]$Matches[1] -le 22 -and [int]$Matches[2] -ge 22) { $portMatches = $true; break }
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
        return ConvertTo-RemoteAccessFirewallState -Rules $normalizedRules.ToArray() -DefaultInboundAction 'Block' -ListenerProgramPaths $listenerPaths.ToArray()
    } catch {
        return 'unknown'
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

    try {
        $adapter = @(Get-NetAdapter -ErrorAction Stop | Where-Object {
            ($_.Name -match '(?i)tailscale') -or ($_.InterfaceDescription -match '(?i)tailscale')
        } | Where-Object Status -eq 'Up')
        $tailnet.interface_present = $adapter.Count -gt 0
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
    $listenerCheck = 'unknown'
    try {
        $null = @(Get-NetTCPConnection -State Listen -LocalPort 22 -ErrorAction Stop)
        $listenerCheck = $now
    } catch { }

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
            firewall = Get-RemoteAccessFirewallState
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
