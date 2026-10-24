#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$WindowsAccount,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$PublicKeyPath,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string[]]$TrustedClientAddresses,
    [ValidateRange(1, 65535)][int]$Port = 22,
    [switch]$ConfirmAlternatePort,
    [switch]$Apply,
    [switch]$FinalizeFirewall,
    [switch]$ConfirmSecondDeviceKeyLogin
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'RemoteAccess.Common.ps1')

function Test-CurrentProcessAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-LocalGroupContainsAccountSid {
    param(
        [Parameter(Mandatory = $true)][string]$GroupName,
        [Parameter(Mandatory = $true)][string]$AccountSid,
        [Parameter(Mandatory = $true)][System.Collections.Generic.HashSet[string]]$Visited
    )

    $group = Get-LocalGroup -Name $GroupName -ErrorAction Stop
    $groupSid = [string]$group.SID.Value
    if (-not $Visited.Add($groupSid)) { return $false }
    foreach ($member in @(Get-LocalGroupMember -Group $group.Name -ErrorAction Stop)) {
        $memberSid = [string]$member.SID.Value
        if ($memberSid -eq $AccountSid) { return $true }
        if ([string]$member.ObjectClass -eq 'Group' -and [string]$member.PrincipalSource -eq 'Local') {
            $nestedName = ([string]$member.Name).Split('\')[-1]
            $nested = Get-LocalGroup -Name $nestedName -ErrorAction SilentlyContinue
            if ($nested -and (Test-LocalGroupContainsAccountSid -GroupName $nested.Name -AccountSid $AccountSid -Visited $Visited)) { return $true }
        }
    }
    return $false
}

function Get-ApprovedLocalAccount {
    param([Parameter(Mandatory = $true)][string]$AccountName)

    $name = $AccountName.Trim()
    if ($name.Contains('\')) {
        $parts = $name.Split('\')
        if ($parts.Count -ne 2 -or $parts[0] -ine $env:COMPUTERNAME) { throw 'Only a local Windows account on this host is supported.' }
        $name = $parts[1]
    }
    if ($name -notmatch '^[A-Za-z0-9_.-]{1,64}$') { throw 'The account name is not in the supported local-account format.' }

    $user = Get-LocalUser -Name $name -ErrorAction Stop
    if (-not $user.Enabled) { throw 'The approved local account is disabled.' }
    $sid = [string]$user.SID.Value
    $membershipGroups = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)

    foreach ($group in @(Get-LocalGroup -ErrorAction Stop)) {
        $visited = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        if (Test-LocalGroupContainsAccountSid -GroupName $group.Name -AccountSid $sid -Visited $visited) {
            [void]$membershipGroups.Add([string]$group.SID.Value)
        }
    }
    $usersSid = 'S-1-5-32-545'
    $adminSid = 'S-1-5-32-544'
    $dockerSid = $null
    try { $dockerSid = [string](Get-LocalGroup -Name 'docker-users' -ErrorAction Stop).SID.Value } catch { }
    if (-not $membershipGroups.Contains($usersSid)) { throw 'The account is not confirmed as a standard local user.' }
    if ($membershipGroups.Contains($adminSid) -or ($dockerSid -and $membershipGroups.Contains($dockerSid))) {
        throw 'The account belongs to an excluded privileged group.'
    }
    foreach ($groupSid in $membershipGroups) {
        if ($groupSid -ne $usersSid) { throw 'The account has additional local-group membership; review it before SSH setup.' }
    }

    $profileKey = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\' + $sid
    $profile = (Get-ItemProperty -LiteralPath $profileKey -Name ProfileImagePath -ErrorAction Stop).ProfileImagePath
    $profile = [Environment]::ExpandEnvironmentVariables([string]$profile)
    if (-not (Test-Path -LiteralPath $profile -PathType Container)) { throw 'The approved account profile is not initialized; sign in locally once before setup.' }
    return [pscustomobject]@{ name = $name; sid = $sid; profile_path = $profile }
}

function Read-PublicKeyLine {
    param([Parameter(Mandatory = $true)][string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    return ConvertFrom-RemoteOpenSshPublicKey -KeyText ([IO.File]::ReadAllText($resolved))
}

function Get-AuthorizedKeyPath {
    param([Parameter(Mandatory = $true)][string]$ProfilePath)
    return (Join-Path (Join-Path $ProfilePath '.ssh') 'authorized_keys')
}

function Assert-RestrictedFileAcl {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string[]]$AllowedSids)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne 'Allow') { continue }
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin $AllowedSids) { throw 'An authorized_keys path grants access to an unapproved principal.' }
    }
}

function Test-OpenSshSystemOnlyAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    $allowed = @('S-1-5-18', 'S-1-5-32-544')
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    if (-not $acl.AreAccessRulesProtected) { return $false }
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne 'Allow') { continue }
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin $allowed) { return $false }
    }
    return $true
}

function Set-OpenSshSystemOnlyAcl {
    param([Parameter(Mandatory = $true)][string]$Path, [switch]$Directory)
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($existing in @($acl.Access)) { [void]$acl.RemoveAccessRuleAll($existing) }
    foreach ($sidText in @('S-1-5-18', 'S-1-5-32-544')) {
        $sid = New-Object Security.Principal.SecurityIdentifier($sidText)
        if ($Directory) {
            $access = New-Object Security.AccessControl.FileSystemAccessRule($sid, [Security.AccessControl.FileSystemRights]::FullControl, 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        } else {
            $access = New-Object Security.AccessControl.FileSystemAccessRule($sid, [Security.AccessControl.FileSystemRights]::FullControl, 'Allow')
        }
        [void]$acl.AddAccessRule($access)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
}

function Get-RemoteSshHostKeyInventory {
    $directoryPath = Join-Path $env:ProgramData 'ssh'
    $directoryExists = Test-Path -LiteralPath $directoryPath -PathType Container
    $existingKeys = New-Object 'System.Collections.Generic.List[object]'
    $safe = $true
    if (Test-Path -LiteralPath $directoryPath) {
        $directoryItem = Get-Item -LiteralPath $directoryPath -Force -ErrorAction Stop
        if (-not $directoryItem.PSIsContainer -or ($directoryItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) { $safe = $false }
        elseif (-not (Test-OpenSshSystemOnlyAcl -Path $directoryPath)) { $safe = $false }
    }
    foreach ($name in @('ssh_host_ecdsa_key', 'ssh_host_ed25519_key', 'ssh_host_rsa_key')) {
        $path = Join-Path $directoryPath $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { $safe = $false; continue }
        if (-not (Test-OpenSshSystemOnlyAcl -Path $path)) { $safe = $false; continue }
        $existingKeys.Add(@{ name = $name; sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash })
    }
    return [pscustomobject]@{ directory_path = $directoryPath; directory_exists = $directoryExists; directory_safe = $safe; existing_keys = @($existingKeys.ToArray()) }
}

function Initialize-RemoteSshHostKeys {
    param([Parameter(Mandatory = $true)][object]$Manifest, [Parameter(Mandatory = $true)][string]$ManifestPath, [Parameter(Mandatory = $true)][object]$BeforeState)

    $directoryPath = [string]$BeforeState.directory_path
    $resource = Get-RemoteAccessField (Get-RemoteAccessField $Manifest 'resources') 'ssh_host_keys'
    if ($null -eq $resource) { throw 'The SSH host-key ownership resource is missing from the protected manifest.' }
    if (-not (Test-Path -LiteralPath $directoryPath -PathType Container)) {
        $script:hostMutationStarted = $true
        New-Item -ItemType Directory -Path $directoryPath -ErrorAction Stop | Out-Null
        Set-OpenSshSystemOnlyAcl -Path $directoryPath -Directory
        $resource.directory_created = $true
        Save-Manifest -Manifest $Manifest -Path $ManifestPath
    } elseif (-not (Test-OpenSshSystemOnlyAcl -Path $directoryPath)) {
        throw 'The OpenSSH data directory ACL changed after review; refusing to generate host keys.'
    }

    $beforeByName = @{}
    foreach ($item in @(Get-RemoteAccessField $resource 'before')) { $beforeByName[[string]$item.name] = [string]$item.sha256 }
    $createdByName = @{}
    foreach ($item in @(Get-RemoteAccessField $resource 'created')) { $createdByName[[string]$item.name] = [string]$item.sha256 }
    $required = @('ssh_host_ecdsa_key', 'ssh_host_ed25519_key', 'ssh_host_rsa_key')
    foreach ($name in $required) {
        $path = Join-Path $directoryPath $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash
        if ($beforeByName.ContainsKey($name) -and $beforeByName[$name] -ne $hash) { throw 'A pre-existing OpenSSH host key changed after review; refusing to replace it.' }
        if ($createdByName.ContainsKey($name) -and $createdByName[$name] -ne $hash) { throw 'A feature-generated OpenSSH host key changed after setup; refusing to overwrite it.' }
        if (-not $beforeByName.ContainsKey($name) -and -not $createdByName.ContainsKey($name)) {
            $script:hostMutationStarted = $true
            Set-OpenSshSystemOnlyAcl -Path $path
            $createdByName[$name] = $hash
        }
    }

    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $directoryPath $_) -PathType Leaf) })
    if ($missing.Count -gt 0) {
        $resource.generating = [string[]]$missing
        $script:hostMutationStarted = $true
        Save-Manifest -Manifest $Manifest -Path $ManifestPath
        $keygenPath = Join-Path $env:SystemRoot 'System32\OpenSSH\ssh-keygen.exe'
        if (-not (Test-Path -LiteralPath $keygenPath -PathType Leaf)) { throw 'The in-box ssh-keygen binary is unavailable; no sshd listener was started.' }
        Push-Location (Split-Path -Parent $keygenPath)
        try { & $keygenPath -A 2>$null; $keygenExit = $LASTEXITCODE } finally { Pop-Location }
        if ($keygenExit -ne 0) { throw 'OpenSSH host-key generation failed; sshd was not started.' }
        foreach ($name in $missing) {
            $path = Join-Path $directoryPath $name
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'OpenSSH did not generate every required host key; sshd was not started.' }
            $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'A generated OpenSSH host key is a reparse point; refusing to continue.' }
            Set-OpenSshSystemOnlyAcl -Path $path
            $createdByName[$name] = (Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash
        }
    }
    $resource.created = @($createdByName.Keys | Sort-Object | ForEach-Object { @{ name = $_; sha256 = $createdByName[$_] } })
    $resource.managed = ($resource.directory_created -eq $true -or @($resource.created).Count -gt 0)
    $resource.generating = @()
    Save-Manifest -Manifest $Manifest -Path $ManifestPath
}

function Test-AuthorizedKeyPresent {
    param([string]$Path, [Parameter(Mandatory = $true)][object]$Key)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line -match ('(?:^|\s)' + [regex]::Escape($Key.type) + '\s+' + [regex]::Escape($Key.data) + '(?:\s|$)')) { return $true }
    }
    return $false
}

function Get-OpenSshDefaultRuleState {
    param([object]$Manifest)
    $owned = Test-RemoteAccessResourceOwned -Manifest $Manifest -Name 'default_openssh_rule'
    $rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
    if (-not $rule) { return 'absent' }
    $port = Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
    $app = Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
    $service = Get-NetFirewallServiceFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
    $address = Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -ErrorAction Stop
    $shape = @{
        name = $rule.Name
        enabled = ([string]$rule.Enabled -eq 'True')
        direction = [string]$rule.Direction
        action = [string]$rule.Action
        protocol = [string]$port.Protocol
        local_port = [string]$port.LocalPort
        remote_addresses = @($address.RemoteAddress | ForEach-Object { [string]$_ })
        program = [string]$app.Program
        service = [string]$service.Service
    }
    if (-not (Test-RemoteSshDefaultFirewallRule -Rule $shape -AllowDisabled:$owned)) { return 'unknown' }
    if ($owned) { return 'feature-managed-broad' }
    return 'preexisting-broad'
}

function Get-SshdServiceState {
    try {
        $service = Get-Service -Name sshd -ErrorAction Stop
        return [string]$service.Status
    } catch {
        return 'missing'
    }
}

function Get-SshFirewallDefaultAllowState {
    try {
        $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
        $state = Get-RemoteAccessFirewallProfileState -Profiles $profiles
        if ($state -eq 'broad') { return $true }
        if ($state -eq 'blocked') { return $false }
        return $null
    } catch {
        return $null
    }
}

function Get-SshdPortInUse {
    param([int]$SelectedPort, [bool]$FeatureOwnsService)
    return Get-RemoteAccessPortUseState -ListenerQuery { param($port) Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop } -Port $SelectedPort -FeatureOwnsListener $FeatureOwnsService -ExpectedProcessName 'sshd'
}

function Get-TailscaleInterfaceFacts {
    param([Parameter(Mandatory = $true)][string[]]$ClientAddresses)
    $interfaces = @(Get-NetAdapter -ErrorAction Stop | Where-Object {
        $_.Status -eq 'Up' -and (($_.Name -match '(?i)tailscale') -or ($_.InterfaceDescription -match '(?i)tailscale'))
    })
    if (-not $interfaces) { throw 'No active Tailscale network interface was found.' }
    $hostAddresses = @()
    foreach ($interface in $interfaces) {
        $hostAddresses += @(Get-NetIPAddress -InterfaceIndex $interface.ifIndex -ErrorAction Stop | ForEach-Object { [string]$_.IPAddress } | Where-Object { Test-RemoteAccessTailnetAddress $_ })
    }
    if (-not $hostAddresses) { throw 'The active Tailscale interface has no verified tailnet address.' }
    foreach ($client in $ClientAddresses) {
        $clientIp = [Net.IPAddress]::Parse($client)
        $hasFamilyAddress = $false
        foreach ($hostAddress in $hostAddresses) {
            if ([Net.IPAddress]::Parse($hostAddress).AddressFamily -eq $clientIp.AddressFamily) { $hasFamilyAddress = $true; break }
        }
        if (-not $hasFamilyAddress) { throw 'The host has no Tailscale interface address for a requested client address family.' }
    }
    return [pscustomobject]@{ aliases = @($interfaces | ForEach-Object { [string]$_.Name }); addresses = @($hostAddresses | Sort-Object -Unique) }
}

function Assert-ProtectedStateDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
        $acl = Get-Acl -LiteralPath $Path
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleAll($rule) }
        foreach ($entry in @(
            @{ sid = 'S-1-5-18'; rights = [Security.AccessControl.FileSystemRights]::FullControl },
            @{ sid = 'S-1-5-32-544'; rights = [Security.AccessControl.FileSystemRights]::FullControl }
        )) {
            $identity = New-Object Security.Principal.SecurityIdentifier($entry.sid)
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, $entry.rights, 'ContainerInherit,ObjectInherit', 'None', 'Allow')
            [void]$acl.AddAccessRule($rule)
        }
        Set-Acl -LiteralPath $Path -AclObject $acl
    } else {
        if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'The remote-access state directory is a reparse point and is not trusted.' }
        $acl = Get-Acl -LiteralPath $Path
        if (-not $acl.AreAccessRulesProtected) { throw 'The existing remote-access state directory inherits permissions and is not trusted.' }
        foreach ($rule in $acl.Access) {
            $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            if ($rule.AccessControlType -eq 'Allow' -and $sid -notin @('S-1-5-18', 'S-1-5-32-544')) { throw 'The existing remote-access state directory grants access outside SYSTEM and Administrators.' }
        }
    }
}

function Save-InitialManifest {
    param(
        [string]$Root,
        [string]$ManifestPath,
        [object]$Account,
        [string]$KeyPath,
        [object]$Preflight,
        [object]$Tailscale,
        [string]$SshdConfigPath,
        [string]$AuthorizedKeysPath,
        [string]$SnapshotPath,
        [Parameter(Mandatory = $true)][object]$HostKeys
    )

    New-Item -ItemType Directory -Path $SnapshotPath -Force | Out-Null
    $configExists = Test-Path -LiteralPath $SshdConfigPath -PathType Leaf
    $keysExist = Test-Path -LiteralPath $AuthorizedKeysPath -PathType Leaf
    $keyDirectory = Split-Path -Parent $AuthorizedKeysPath
    $keyDirectoryExists = Test-Path -LiteralPath $keyDirectory -PathType Container
    $configBackup = $null
    $keysBackup = $null
    $keysSddl = $null
    $configSddl = $null
    if ($configExists) {
        $configBackup = Join-Path $SnapshotPath 'sshd_config.before'
        Copy-Item -LiteralPath $SshdConfigPath -Destination $configBackup -Force
        $configSddl = (Get-Acl -LiteralPath $SshdConfigPath).Sddl
    }
    if ($keysExist) {
        $keysBackup = Join-Path $SnapshotPath 'authorized_keys.before'
        Copy-Item -LiteralPath $AuthorizedKeysPath -Destination $keysBackup -Force
        $keysSddl = (Get-Acl -LiteralPath $AuthorizedKeysPath).Sddl
    }

    $service = $null
    try {
        $existingService = Get-Service -Name sshd -ErrorAction Stop
        $service = @{ existed = $true; status = [string]$existingService.Status; start_type = [string](Get-CimInstance Win32_Service -Filter "Name='sshd'" -ErrorAction Stop).StartMode }
    } catch { $service = @{ existed = $false; status = 'missing'; start_type = 'missing' } }
    $capability = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop
    $defaultRule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
    $manifest = [pscustomobject]@{
        schema_version = 1
        component = 'ssh'
        state = 'applying'
        created_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
        account = @{ name = $Account.name; sid = $Account.sid; profile_path = $Account.profile_path }
        port = [int]$Preflight.port
        client_addresses = @($Preflight.trusted_client_addresses)
        listener_addresses = @($Tailscale.addresses)
        listener_interfaces = @($Tailscale.aliases)
        public_key_sha256 = (Get-FileHash -LiteralPath $KeyPath -Algorithm SHA256).Hash
        snapshot_path = $SnapshotPath
        resources = @{
            ssh_capability = @{ managed = ([string]$capability.State -ne 'Installed'); was_installed = ([string]$capability.State -eq 'Installed') }
            sshd_config = @{ managed = $true; existed = $configExists; configured = $false; backup_path = $configBackup; package_default_backup = $null; path = $SshdConfigPath; sddl = $configSddl; sha256 = $null }
            authorized_keys = @{ managed = $true; existed = $keysExist; backup_path = $keysBackup; sddl = $keysSddl; path = $AuthorizedKeysPath; directory_existed = $keyDirectoryExists; directory_path = $keyDirectory; sha256_after = $null }
            ssh_host_keys = @{ managed = $false; path = $HostKeys.directory_path; directory_existed = [bool]$HostKeys.directory_exists; directory_created = $false; before = @($HostKeys.existing_keys); created = @(); generating = @() }
            ssh_service = @{ managed = $true; before = $service }
            ssh_firewall_v4 = @{ managed = $false; name = 'AIStack-RemoteAccess-SSH-v4'; expected = $null }
            ssh_firewall_v6 = @{ managed = $false; name = 'AIStack-RemoteAccess-SSH-v6'; expected = $null }
            default_openssh_rule = @{ managed = $false; existed_before = ($null -ne $defaultRule); enabled_before = if ($defaultRule) { [string]$defaultRule.Enabled } else { 'absent' }; name = 'OpenSSH-Server-In-TCP' }
        }
    }
    $manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $ManifestPath -Encoding UTF8
    return $manifest
}

function Save-Manifest {
    param([object]$Manifest, [string]$Path)
    $Manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Set-RemoteSshFirewallRules {
    param([object]$Manifest, [string[]]$ClientAddresses, [object]$Tailscale, [string]$SshdPath)
    foreach ($family in @('v4', 'v6')) {
        $ruleName = "AIStack-RemoteAccess-SSH-$family"
        $familyRemotes = @($ClientAddresses | Where-Object {
            $ip = [Net.IPAddress]::Parse($_)
            if ($family -eq 'v4') { $ip.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork } else { $ip.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6 }
        })
        $familyLocals = @($Tailscale.addresses | Where-Object {
            $ip = [Net.IPAddress]::Parse($_)
            if ($family -eq 'v4') { $ip.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork } else { $ip.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6 }
        })
        if (-not $familyRemotes) { continue }
        $existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
        $desired = @{
            enabled = 'True'
            direction = 'Inbound'
            action = 'Allow'
            profile = 'Any'
            protocol = 'TCP'
            local_port = [string]$Manifest.port
            remote_addresses = @($familyRemotes)
            local_addresses = @($familyLocals)
            interface_aliases = @($Tailscale.aliases)
            program = $SshdPath
        }
        if ($existing) {
            if (-not (Test-RemoteAccessResourceOwned -Manifest $Manifest -Name "ssh_firewall_$family")) { throw 'A firewall rule with the feature name already exists but is not feature-managed.' }
            $current = Get-RemoteAccessFirewallRuleSnapshot -Rule $existing
            $resource = Get-RemoteAccessField (Get-RemoteAccessField $Manifest 'resources') "ssh_firewall_$family"
            if (-not (Test-RemoteAccessFirewallRuleSnapshot -Actual $current -Expected $resource.expected) -or -not (Test-RemoteAccessFirewallRuleSnapshot -Actual $current -Expected $desired)) {
                throw 'A feature-managed SSH firewall rule differs from its recorded or requested scope; refusing to change it.'
            }
            continue
        }
        $resource = Get-RemoteAccessField (Get-RemoteAccessField $Manifest 'resources') "ssh_firewall_$family"
        $resource.managed = $true
        $resource.expected = $desired
        Save-Manifest $Manifest (Join-Path $env:ProgramData 'AI-Stack\RemoteAccess\ssh-manifest.json')
        New-NetFirewallRule -Name $ruleName -DisplayName 'AI-Stack tailnet SSH recovery' -Direction Inbound -Action Allow -Enabled True -Profile Any -Protocol TCP -LocalPort ([int]$Manifest.port) -LocalAddress $familyLocals -RemoteAddress $familyRemotes -InterfaceAlias $Tailscale.aliases -Program $SshdPath -ErrorAction Stop | Out-Null
        $created = Get-NetFirewallRule -Name $ruleName -ErrorAction Stop
        $actual = Get-RemoteAccessFirewallRuleSnapshot -Rule $created
        $resource.expected = $actual
        Save-Manifest $Manifest (Join-Path $env:ProgramData 'AI-Stack\RemoteAccess\ssh-manifest.json')
        if (-not (Test-RemoteAccessFirewallRuleSnapshot -Actual $actual -Expected $desired)) { throw 'The created firewall rule does not match the exact requested tailnet scope.' }
    }
}

function Add-ApprovedAuthorizedKey {
    param([string]$Path, [string]$DirectoryPath, [object]$Account, [object]$Key, [object]$Manifest)
    if (Test-AuthorizedKeyPresent -Path $Path -Key $Key) { return }
    Assert-RestrictedFileAcl -Path $DirectoryPath -AllowedSids @($Account.sid, 'S-1-5-18', 'S-1-5-32-544')
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Assert-RestrictedFileAcl -Path $Path -AllowedSids @($Account.sid, 'S-1-5-18', 'S-1-5-32-544')
    } else {
        if (-not (Test-Path -LiteralPath $DirectoryPath -PathType Container)) {
            New-Item -ItemType Directory -Path $DirectoryPath -Force | Out-Null
            $acl = Get-Acl -LiteralPath $DirectoryPath
            $acl.SetAccessRuleProtection($true, $false)
            foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleAll($rule) }
            foreach ($sidText in @($Account.sid, 'S-1-5-18', 'S-1-5-32-544')) {
                $sid = New-Object Security.Principal.SecurityIdentifier($sidText)
                $access = New-Object Security.AccessControl.FileSystemAccessRule($sid, [Security.AccessControl.FileSystemRights]::FullControl, 'ContainerInherit,ObjectInherit', 'None', 'Allow')
                [void]$acl.AddAccessRule($access)
            }
            Set-Acl -LiteralPath $DirectoryPath -AclObject $acl
        }
        New-Item -ItemType File -Path $Path -Force | Out-Null
        $acl = Get-Acl -LiteralPath $Path
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRuleAll($rule) }
        foreach ($sidText in @($Account.sid, 'S-1-5-18', 'S-1-5-32-544')) {
            $sid = New-Object Security.Principal.SecurityIdentifier($sidText)
            $access = New-Object Security.AccessControl.FileSystemAccessRule($sid, [Security.AccessControl.FileSystemRights]::FullControl, 'Allow')
            [void]$acl.AddAccessRule($access)
        }
        Set-Acl -LiteralPath $Path -AclObject $acl
    }
    [IO.File]::AppendAllText($Path, $Key.line + "`r`n", [Text.Encoding]::ASCII)
}

function Get-RemoteSshConfigPath {
    return (Join-Path $env:ProgramData 'ssh\sshd_config')
}

function Write-RemoteSshConfig {
    param([string]$Path, [string]$Text)
    $parent = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw 'The OpenSSH configuration directory is unavailable.' }
    [IO.File]::WriteAllText($Path, $Text, (New-Object Text.UTF8Encoding($false)))
}

function Get-FeatureManifest {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { $manifest = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -ErrorAction Stop } catch { throw 'The protected SSH ownership manifest is invalid; refusing to continue.' }
    if ($manifest.schema_version -ne 1 -or $manifest.component -ne 'ssh' -or $manifest.state -notin @('applying', 'active')) { throw 'The protected SSH ownership manifest has an unsupported or non-reusable state.' }
    return $manifest
}

function Write-InstallPlan {
    param([object]$Plan)
    [pscustomobject]@{
        ready = $Plan.ready
        apply_requested = [bool]$Apply
        port = $Plan.port
        blockers = [string[]]@($Plan.blockers)
        warnings = [string[]]@($Plan.warnings)
        actions = [string[]]@($Plan.actions)
        state = if ($Apply) { 'preflight' } else { 'plan-only' }
    } | ConvertTo-Json -Depth 5
}

$stateRoot = Join-Path $env:ProgramData 'AI-Stack\RemoteAccess'
$manifestPath = Join-Path $stateRoot 'ssh-manifest.json'
$sshdConfigPath = Get-RemoteSshConfigPath
$keyPath = $null
$manifest = $null
$clientKey = $null
$account = $null
$profilePath = $null
$authorizedKeysPath = $null
$directoryPath = $null
$portInUse = $null
$defaultRuleState = 'unknown'
$firewallState = 'unknown'
$firewallDefaultAllow = $null
$serviceState = 'unknown'
$preflight_error = $null
$tailscale = $null
$accountIsStandard = $false
$publicKeyValid = $false
$manifestScopeMatches = $null
$hostKeyFacts = $null
$hostKeyPlan = $null

try {
    $manifest = Get-FeatureManifest -Path $manifestPath
    $account = Get-ApprovedLocalAccount -AccountName $WindowsAccount
    $accountIsStandard = $true
    $clientKey = Read-PublicKeyLine -Path $PublicKeyPath
    $keyPath = (Resolve-Path -LiteralPath $PublicKeyPath).Path
    $publicKeyValid = $true
    $hostKeyFacts = Get-RemoteSshHostKeyInventory
    $hostKeyPlan = Get-RemoteSshHostKeySetupPlan -DirectorySafe $hostKeyFacts.directory_safe -DirectoryExists $hostKeyFacts.directory_exists -ExistingHostKeys @($hostKeyFacts.existing_keys | ForEach-Object { [string]$_.name })
    $profilePath = $account.profile_path
    $authorizedKeysPath = Get-AuthorizedKeyPath -ProfilePath $profilePath
    $directoryPath = Split-Path -Parent $authorizedKeysPath
    Assert-RestrictedFileAcl -Path $directoryPath -AllowedSids @($account.sid, 'S-1-5-18', 'S-1-5-32-544')
    Assert-RestrictedFileAcl -Path $authorizedKeysPath -AllowedSids @($account.sid, 'S-1-5-18', 'S-1-5-32-544')
    $tailscale = Get-TailscaleInterfaceFacts -ClientAddresses $TrustedClientAddresses
    $manifestScopeMatches = Test-RemoteSshManifestScope -Manifest $manifest -AccountSid $account.sid -Port $Port -ClientAddresses $TrustedClientAddresses -ListenerAddresses $tailscale.addresses -InterfaceAliases $tailscale.aliases
    $defaultRuleState = Get-OpenSshDefaultRuleState -Manifest $manifest
    $serviceState = Get-SshdServiceState
    $featureOwnsService = Test-RemoteAccessResourceOwned -Manifest $manifest -Name 'ssh_service'
    $portInUse = Get-SshdPortInUse -SelectedPort $Port -FeatureOwnsService $featureOwnsService
    $firewallDefaultAllow = Get-SshFirewallDefaultAllowState
    $firewallState = Get-RemoteAccessFirewallState -Port $Port -ExpectedProgramPaths @((Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'))
    $customConfig = (Test-Path -LiteralPath $sshdConfigPath -PathType Leaf) -and -not (Test-RemoteAccessResourceOwned -Manifest $manifest -Name 'sshd_config')
    $keyExists = Test-AuthorizedKeyPresent -Path $authorizedKeysPath -Key $clientKey
    $preflight = @{
        account_is_standard = $accountIsStandard
        public_key_valid = $publicKeyValid
        manifest_scope_matches = $manifestScopeMatches
        host_key_path_safe = [bool]$hostKeyPlan.ready
        trusted_client_addresses = @($TrustedClientAddresses)
        port = $Port
        alternate_port_approved = ($ConfirmAlternatePort -or $Port -eq 22)
        port_in_use = $portInUse
        custom_sshd_config = $customConfig
        authorized_key_exists = $keyExists
        service_state = $serviceState
        firewall_state = $firewallState
        firewall_default_allow = $firewallDefaultAllow
        default_openssh_rule_state = $defaultRuleState
        finalize_firewall = [bool]$FinalizeFirewall
        second_device_verified = [bool]$ConfirmSecondDeviceKeyLogin
        broad_rule_manifested = (Test-RemoteAccessResourceOwned -Manifest $manifest -Name 'default_openssh_rule')
    }
} catch {
    $preflight = @{
        account_is_standard = $accountIsStandard
        public_key_valid = $publicKeyValid
        manifest_scope_matches = $manifestScopeMatches
        host_key_path_safe = $false
        trusted_client_addresses = @($TrustedClientAddresses)
        port = $Port
        alternate_port_approved = ($ConfirmAlternatePort -or $Port -eq 22)
        port_in_use = $portInUse
        custom_sshd_config = $false
        authorized_key_exists = $false
        service_state = $serviceState
        firewall_state = $firewallState
        firewall_default_allow = $firewallDefaultAllow
        default_openssh_rule_state = $defaultRuleState
        finalize_firewall = [bool]$FinalizeFirewall
        second_device_verified = [bool]$ConfirmSecondDeviceKeyLogin
        broad_rule_manifested = $false
    }
    $preflight_error = 'host-prerequisite-check-failed'
}

$plan = Get-RemoteSshInstallPlan -Preflight $preflight
if ($preflight_error) {
    $plan = [pscustomobject]@{
        ready = $false
        port = $Port
        blockers = @($plan.blockers + $preflight_error)
        warnings = @($plan.warnings)
        actions = @($plan.actions)
    }
}
Write-InstallPlan -Plan $plan
if (-not $Apply) { return }
if (-not $plan.ready) { Write-Error 'SSH setup preflight did not pass; no local changes were made.'; exit 1 }
if (-not (Test-CurrentProcessAdministrator)) { Write-Error 'Run the explicit apply from an elevated PowerShell session; no local changes were made.'; exit 1 }
if ($FinalizeFirewall -and -not $ConfirmSecondDeviceKeyLogin) { Write-Error 'Firewall finalization requires -ConfirmSecondDeviceKeyLogin; no local changes were made.'; exit 1 }
if (-not $PSCmdlet.ShouldProcess('Windows OpenSSH on this host', 'Apply the approved tailnet-scoped SSH plan')) { return }

$previousManifestState = if ($manifest) { [string]$manifest.state } else { $null }
$hostMutationStarted = $false
$startedSshd = $false
try {
    Assert-ProtectedStateDirectory -Path $stateRoot
    if (-not $manifest) {
        $snapshotPath = Join-Path $stateRoot ('snapshots\ssh-' + [Guid]::NewGuid().ToString('N'))
        $manifest = Save-InitialManifest -Root $stateRoot -ManifestPath $manifestPath -Account $account -KeyPath $keyPath -Preflight $preflight -Tailscale $tailscale -SshdConfigPath $sshdConfigPath -AuthorizedKeysPath $authorizedKeysPath -SnapshotPath $snapshotPath -HostKeys $hostKeyFacts
    } elseif (-not (Test-RemoteSshManifestScope -Manifest $manifest -AccountSid $account.sid -Port $Port -ClientAddresses $TrustedClientAddresses -ListenerAddresses $tailscale.addresses -InterfaceAliases $tailscale.aliases)) {
        throw 'The existing SSH manifest scope changed; use the documented rollback and a new reviewed apply to change account, port, client, or Tailscale interface scope.'
    }

    $capability = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop
    if ([string]$capability.State -ne 'Installed') {
        $hostMutationStarted = $true
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop | Out-Null
    }

    if ($hostKeyPlan.create_directory -or @($hostKeyPlan.missing_keys).Count -gt 0) { $hostMutationStarted = $true }
    Initialize-RemoteSshHostKeys -Manifest $manifest -ManifestPath $manifestPath -BeforeState $hostKeyFacts

    $defaultRule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
    if ($defaultRule) {
        $ruleShape = @{
            name = $defaultRule.Name
            enabled = ([string]$defaultRule.Enabled -eq 'True')
            direction = [string]$defaultRule.Direction
            action = [string]$defaultRule.Action
            protocol = [string](Get-NetFirewallPortFilter -AssociatedNetFirewallRule $defaultRule).Protocol
            local_port = [string](Get-NetFirewallPortFilter -AssociatedNetFirewallRule $defaultRule).LocalPort
            remote_addresses = @((Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $defaultRule).RemoteAddress | ForEach-Object { [string]$_ })
            program = [string](Get-NetFirewallApplicationFilter -AssociatedNetFirewallRule $defaultRule).Program
            service = [string](Get-NetFirewallServiceFilter -AssociatedNetFirewallRule $defaultRule).Service
        }
        $defaultResource = Get-RemoteAccessField (Get-RemoteAccessField $manifest 'resources') 'default_openssh_rule'
        if (-not (Test-RemoteSshDefaultRuleForApply -Rule $ruleShape -Resource $defaultResource)) { throw 'The Windows OpenSSH firewall rule has an unrecognized shape; refusing to change it.' }
        if (-not $defaultResource.existed_before) {
            $hostMutationStarted = $true
            $defaultResource.managed = $true
            Save-Manifest $manifest $manifestPath
            Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled False -ErrorAction Stop | Out-Null
            Save-Manifest $manifest $manifestPath
        } elseif ($defaultResource.managed -and $FinalizeFirewall -and $ConfirmSecondDeviceKeyLogin) {
            $hostMutationStarted = $true
            Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled False -ErrorAction Stop | Out-Null
            $defaultResource.managed = $true
            Save-Manifest $manifest $manifestPath
        } elseif ([string]$defaultRule.Enabled -eq 'True') {
            throw 'A pre-existing broad Windows OpenSSH firewall rule remains enabled; it was not changed.'
        }
    }

    $keyResource = Get-RemoteAccessField (Get-RemoteAccessField $manifest 'resources') 'authorized_keys'
    if ($keyResource.sha256_after -and (Test-Path -LiteralPath $authorizedKeysPath -PathType Leaf)) {
        $currentKeyFileHash = (Get-FileHash -LiteralPath $authorizedKeysPath -Algorithm SHA256).Hash
        if ($currentKeyFileHash -ne [string]$keyResource.sha256_after) { throw 'The feature-managed authorized_keys file has local edits; refusing to overwrite them.' }
    }
    $existingKey = Test-AuthorizedKeyPresent -Path $authorizedKeysPath -Key $clientKey
    if (-not $existingKey) { $hostMutationStarted = $true; Add-ApprovedAuthorizedKey -Path $authorizedKeysPath -DirectoryPath $directoryPath -Account $account -Key $clientKey -Manifest $manifest }
    $keyResource.sha256_after = (Get-FileHash -LiteralPath $authorizedKeysPath -Algorithm SHA256).Hash
    Save-Manifest $manifest $manifestPath

    $configResource = Get-RemoteAccessField (Get-RemoteAccessField $manifest 'resources') 'sshd_config'
    if ($configResource.configured -eq $true -and (Test-Path -LiteralPath $sshdConfigPath -PathType Leaf)) {
        $currentConfigHash = (Get-FileHash -LiteralPath $sshdConfigPath -Algorithm SHA256).Hash
        if ($currentConfigHash -ne [string]$configResource.sha256) { throw 'The feature-managed sshd_config has local edits; refusing to overwrite them.' }
    } else {
        if (Test-Path -LiteralPath $sshdConfigPath -PathType Leaf) {
            if ($configResource.existed -or -not (Test-RemoteAccessResourceOwned -Manifest $manifest -Name 'ssh_capability')) {
                throw 'An existing sshd_config appeared during apply; refusing to replace it.'
            }
            if (-not $configResource.package_default_backup) {
                $configResource.package_default_backup = Join-Path $manifest.snapshot_path 'sshd_config.package-default'
                Copy-Item -LiteralPath $sshdConfigPath -Destination $configResource.package_default_backup -Force
                Save-Manifest $manifest $manifestPath
            }
        }
        $config = New-RemoteSshManagedConfig -Port $Port -WindowsAccount $account.name -ListenAddresses $tailscale.addresses
        $hostMutationStarted = $true
        Write-RemoteSshConfig -Path $sshdConfigPath -Text $config
        $configResource.configured = $true
        $configResource.sha256 = (Get-FileHash -LiteralPath $sshdConfigPath -Algorithm SHA256).Hash
        Save-Manifest $manifest $manifestPath
    }
    $sshdPath = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'
    if (-not (Test-Path -LiteralPath $sshdPath -PathType Leaf)) { throw 'The in-box sshd binary is unavailable after capability installation.' }
    & $sshdPath -t -f $sshdConfigPath 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'The managed sshd configuration did not pass sshd -t; the service was not started.' }

    $hostMutationStarted = $true
    Set-RemoteSshFirewallRules -Manifest $manifest -ClientAddresses $TrustedClientAddresses -Tailscale $tailscale -SshdPath $sshdPath
    $firewallAfter = Get-RemoteAccessFirewallState -Port $Port -ExpectedProgramPaths @($sshdPath)
    if ($firewallAfter -ne 'tailnet-only') { throw 'The effective SSH firewall policy is not proven tailnet-only after rule creation; sshd was not started.' }

    $service = Get-Service -Name sshd -ErrorAction Stop
    if ([string]$service.Status -ne 'Running') {
        $hostMutationStarted = $true
        Set-Service -Name sshd -StartupType Automatic -ErrorAction Stop
        Start-Service -Name sshd -ErrorAction Stop
        $startedSshd = $true
    }
    $firewallAfter = Get-RemoteAccessFirewallState -Port $Port -ExpectedProgramPaths @($sshdPath)
    if ($firewallAfter -ne 'tailnet-only') {
        if ($startedSshd) { Stop-Service -Name sshd -ErrorAction SilentlyContinue }
        throw 'The effective SSH firewall policy is not proven tailnet-only; the newly started service was stopped.'
    }
    $manifest.state = 'active'
    Save-Manifest $manifest $manifestPath
    [pscustomobject]@{ state = 'applied'; ready = $true; port = $Port; firewall = $firewallAfter; manifest = 'protected-local-state' } | ConvertTo-Json -Depth 4
} catch {
    if ($manifest) { $manifest.state = Get-RemoteAccessManifestFailureState -PreviousState $previousManifestState -MutationStarted $hostMutationStarted; Save-Manifest $manifest $manifestPath }
    Write-Error 'SSH setup stopped safely. Protected recovery state is available; no unrelated host resources were changed. Inspect the local protected manifest before retrying.'
    exit 1
}
