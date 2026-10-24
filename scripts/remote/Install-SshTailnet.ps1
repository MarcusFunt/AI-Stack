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
        $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop | Where-Object { $_.Enabled })
        if (-not $profiles) { return $null }
        $actions = @($profiles | ForEach-Object { $_.DefaultInboundAction.ToString() } | Sort-Object -Unique)
        if ($actions -contains 'Allow') { return $true }
        if (@($actions | Where-Object { $_ -ne 'Block' }).Count -gt 0) { return $null }
        return $false
    } catch {
        return $null
    }
}

function Get-SshdPortInUse {
    param([int]$SelectedPort, [bool]$FeatureOwnsService)
    try {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $SelectedPort -ErrorAction SilentlyContinue)
        if (-not $listeners) { return $false }
        if ($FeatureOwnsService) {
            $allSshd = $true
            foreach ($listener in $listeners) {
                try {
                    $process = Get-Process -Id ([int]$listener.OwningProcess) -ErrorAction Stop
                    if ($process.ProcessName -ine 'sshd') { $allSshd = $false }
                } catch { $allSshd = $false }
            }
            if ($allSshd) { return $false }
        }
        return $true
    } catch {
        return $null
    }
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
        [string]$SnapshotPath
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

try {
    $manifest = Get-FeatureManifest -Path $manifestPath
    $account = Get-ApprovedLocalAccount -AccountName $WindowsAccount
    $accountIsStandard = $true
    $clientKey = Read-PublicKeyLine -Path $PublicKeyPath
    $keyPath = (Resolve-Path -LiteralPath $PublicKeyPath).Path
    $publicKeyValid = $true
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

$startedSshd = $false
try {
    Assert-ProtectedStateDirectory -Path $stateRoot
    if (-not $manifest) {
        $snapshotPath = Join-Path $stateRoot ('snapshots\ssh-' + [Guid]::NewGuid().ToString('N'))
        $manifest = Save-InitialManifest -Root $stateRoot -ManifestPath $manifestPath -Account $account -KeyPath $keyPath -Preflight $preflight -Tailscale $tailscale -SshdConfigPath $sshdConfigPath -AuthorizedKeysPath $authorizedKeysPath -SnapshotPath $snapshotPath
    } elseif (-not (Test-RemoteSshManifestScope -Manifest $manifest -AccountSid $account.sid -Port $Port -ClientAddresses $TrustedClientAddresses -ListenerAddresses $tailscale.addresses -InterfaceAliases $tailscale.aliases)) {
        throw 'The existing SSH manifest scope changed; use the documented rollback and a new reviewed apply to change account, port, client, or Tailscale interface scope.'
    }

    $capability = Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop
    if ([string]$capability.State -ne 'Installed') {
        Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop | Out-Null
    }

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
        if (-not (Test-RemoteSshDefaultFirewallRule -Rule $ruleShape)) { throw 'The Windows OpenSSH firewall rule has an unrecognized shape; refusing to change it.' }
        $defaultResource = Get-RemoteAccessField (Get-RemoteAccessField $manifest 'resources') 'default_openssh_rule'
        if (-not $defaultResource.existed_before) {
            Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Enabled False -ErrorAction Stop | Out-Null
            $defaultResource.managed = $true
            Save-Manifest $manifest $manifestPath
        } elseif ($defaultResource.managed -and $FinalizeFirewall -and $ConfirmSecondDeviceKeyLogin) {
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
    if (-not $existingKey) { Add-ApprovedAuthorizedKey -Path $authorizedKeysPath -DirectoryPath $directoryPath -Account $account -Key $clientKey -Manifest $manifest }
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
        Write-RemoteSshConfig -Path $sshdConfigPath -Text $config
        $configResource.configured = $true
        $configResource.sha256 = (Get-FileHash -LiteralPath $sshdConfigPath -Algorithm SHA256).Hash
        Save-Manifest $manifest $manifestPath
    }
    $sshdPath = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'
    if (-not (Test-Path -LiteralPath $sshdPath -PathType Leaf)) { throw 'The in-box sshd binary is unavailable after capability installation.' }
    & $sshdPath -t -f $sshdConfigPath 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'The managed sshd configuration did not pass sshd -t; the service was not started.' }

    Set-RemoteSshFirewallRules -Manifest $manifest -ClientAddresses $TrustedClientAddresses -Tailscale $tailscale -SshdPath $sshdPath
    $firewallAfter = Get-RemoteAccessFirewallState -Port $Port -ExpectedProgramPaths @($sshdPath)
    if ($firewallAfter -eq 'broad') { throw 'The effective SSH firewall policy is broad after rule creation; sshd was not started.' }

    $service = Get-Service -Name sshd -ErrorAction Stop
    if ([string]$service.Status -ne 'Running') {
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
    if ($manifest) { $manifest.state = 'applying'; Save-Manifest $manifest $manifestPath }
    Write-Error 'SSH setup stopped safely. Protected recovery state is available; no unrelated host resources were changed. Inspect the local protected manifest before retrying.'
    exit 1
}
