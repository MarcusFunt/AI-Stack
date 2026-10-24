# Remote Recovery Runbook

**Status:** SSH and `rdc` provisioning code is implemented, but no host setting has been applied. Remote SSH, `rdc`, and a human fallback have not been accepted on a second device.

## Before using this runbook

- Use only an operator-approved Windows account, SSH key, and tailnet client. Keep private keys, identities, source addresses, firewall exports, audit lines, and rollback snapshots outside Git.
- Direct SSH must remain usable when Docker, Gateway, MCP, the dashboard, or the host agent is unavailable.
- Do not use Tailscale Funnel, router forwarding, or public MCP for SSH or desktop access.
- The current Windows TCP 22 listener belongs to `wslrelay`; Windows `sshd` is absent. Do not stop WSL or change that listener. A safe port decision is still required.
- The current signed-in account belongs to Administrators and `docker-users`; it is not approved for agent SSH. A separate least-privilege account is unresolved.
- The installer only plans by default. It requires a standard local account, a public-key file, exact verified Tailscale client IPs, and an explicitly approved available port. It refuses TCP 22 while `wslrelay` owns it.
- `sshd` binds only to the host's current Tailscale IP addresses. Its firewall rules use those local addresses, the Tailscale interface, and the exact approved client IPs. Password authentication is disabled before the service starts.
- If capability installation creates Windows' broad `OpenSSH-Server-In-TCP` rule, the installer disables that newly created rule before starting `sshd`. A pre-existing broad rule blocks setup and is preserved.
- The stack was stopped during the baseline and supervisor status was unavailable. If GPU/job state is unknown, do not start, stop, rebuild, or restart services.

## SSH setup (after operator review)

Choose a standard local account and test client, verify the client's current Tailscale IPv4/IPv6 address, select a free port, and confirm router/Tailscale policy does not expose it publicly. Keep the client public key in a protected local file. Use the same approved values for planning and apply; values are not written to console output or committed.
Replace every angle-bracket placeholder below with the approved value before running the command. Include one exact IP per authorized client address; add both address families only when each was verified.

```powershell
# Read-only plan; port 22 is expected to be rejected while wslrelay owns it.
.\scripts\remote\Install-SshTailnet.ps1 `
  -WindowsAccount '<approved-local-account>' `
  -PublicKeyPath '<protected-client-public-key-file>' `
  -TrustedClientAddresses @('<verified-tailnet-client-ip>') `
  -Port <operator-approved-free-port> -ConfirmAlternatePort

# Only after reviewing the plan and confirming the client/account/port.
.\scripts\remote\Install-SshTailnet.ps1 `
  -WindowsAccount '<approved-local-account>' `
  -PublicKeyPath '<protected-client-public-key-file>' `
  -TrustedClientAddresses @('<verified-tailnet-client-ip>') `
  -Port <operator-approved-free-port> -ConfirmAlternatePort -Apply
```

Run the second-device acceptance checks before relying on SSH: correct key succeeds, wrong key fails, an unapproved peer is denied, and direct internet access is closed. If a feature-owned broad default rule must be finalized, use `-FinalizeFirewall -ConfirmSecondDeviceKeyLogin` only after the successful key login. The confirmation switch records an operator attestation; it is not an automated test result.

The feature manifest and protected before-state are stored under `%ProgramData%\AI-Stack\RemoteAccess`. Inspect the rollback plan first. Apply rollback only from local console or after a separate management path has been verified:

```powershell
.\scripts\remote\Rollback-RemoteAccess.ps1 -Component Ssh
.\scripts\remote\Rollback-RemoteAccess.ps1 -Component Ssh -Apply -ConfirmManagementPath
```

Rollback retains its protected snapshot and refuses paths/rules that are not marked feature-managed. It will not revoke client keys or alter Tailscale Serve, grants, unattended mode, router forwarding, or the WSL listener.

## `rdc` desktop setup (after SSH acceptance)

Only continue after key-only SSH has passed the wrong-key, unauthorized-peer, sign-out, and cold-reboot checks from a second tailnet device. The installer also requires live tailnet-only SSH readiness and the explicit `-ConfirmSshRecoveryAccepted` attestation. That switch records your acceptance; it does not run those tests for you.

Use the immutable release and digest recorded in [the version pin](../plans/remote-access-versions.md). Download `rdc-windows-x86_64.zip` from that pinned release, verify its SHA-256 against the pin, and retain the archive outside the repository. The binary is unsigned and Windows SmartScreen may warn the first time it runs.

Run the installer from the signed-in desktop user's PowerShell. It needs an Administrator token to add the firewall rule; the scheduled task itself is registered without `--elevated` and must remain at standard integrity. The script never changes Tailscale access policy. Replace placeholders with exact identities that you verified in the tailnet. `AllowedIdentities` receive `view` and `input`; `ViewOnlyIdentities` receive `view` only. Leave out the view-only parameter when there are no view-only identities.

```powershell
# Sanitized plan; no host setting changes.
.\scripts\remote\Install-RdcDesktop.ps1 `
  -AllowedIdentities @('<verified-login-or-tag>') `
  -ViewOnlyIdentities @('<optional-verified-view-only-login-or-tag>') `
  -ArchivePath '<path-to-verified-rdc-windows-x86_64.zip>' `
  -BindAddress '<this-hosts-exact-Tailscale-address>' `
  -ConfirmSshRecoveryAccepted

# Apply only after reviewing the plan and confirming the recovery tests above.
.\scripts\remote\Install-RdcDesktop.ps1 `
  -AllowedIdentities @('<verified-login-or-tag>') `
  -ViewOnlyIdentities @('<optional-verified-view-only-login-or-tag>') `
  -ArchivePath '<path-to-verified-rdc-windows-x86_64.zip>' `
  -BindAddress '<this-hosts-exact-Tailscale-address>' `
  -ConfirmSshRecoveryAccepted -Apply
```

The installer refuses wildcards, empty identities, changed managed scope, a conflicting task/config/rule, occupied port, broad firewall state, unknown inbound policy, signed-out invocation, or a missing SSH acceptance gate. It verifies the archive SHA-256 before extracting only `rdc.exe`, creates a protected user-local config and manifest, scopes TCP 7770 to the selected Tailscale address/interface and tailnet source prefixes, then invokes `rdc service install` without `--elevated`. An unchanged healthy install is left running.

Add the matching Tailscale policy `grants`/`acls` rule for the identities and TCP 7770 if the current policy requires it. Match the same exact login/tag identities as the local `rdc` allowlist; the installer will not edit the tailnet policy. Validate from the client with `whoami`, then read-only screenshots/window listing, then a disposable screenshot-click-screenshot test. Verify a view-only identity is denied input and that the audit entry is present. Test lock/sign-out/sign-in and task recovery while SSH remains usable.

The standard task cannot control elevated windows (Windows UIPI drops synthetic input) and cannot reach secure-desktop UAC prompts. `rdc` is unavailable while the user is signed out or locked. The pinned Windows guide was verified on one display at 150% scaling; multi-monitor behavior still needs host testing. Do not report desktop readiness from a session-0 or SSH probe.

Inspect the desktop rollback first. Apply it only as the same Windows user, after SSH or local console management is verified; it also requires Administrator access to remove the firewall rule. It removes only the task, firewall rule, config, and binary whose paths, hashes, task settings, and firewall scope still match the user-local manifest. It retains a rollback manifest.

Plan: .\scripts\remote\Rollback-RdcDesktop.ps1
Apply: .\scripts\remote\Rollback-RdcDesktop.ps1 -Apply -ConfirmManagementPath

## Recovery state order

1. **Host/network:** verify Windows is online and Tailscale is connected from the second device. The current CLI snapshot reported tailnet-only routes on 8443–8449 and 10000; preserve all of them.
2. **SSH boot recovery:** use the approved key and port only after the second-device, sign-out, and cold-reboot acceptance checks pass. Those checks have not passed yet.
3. **AI-Stack application:** on the host, use `scripts\ai.ps1 status` for read-only status. Confirm supervisor/lease state before any lifecycle operation. A down supervisor means active-job state is unknown; escalate to local console instead of guessing.
4. **Desktop:** `rdc` is expected to work only for the signed-in interactive user. Treat signed-out, locked/secure-desktop, and session-0 reports as unavailable unless actual testing proves otherwise.
5. **Human fallback:** no fallback is selected or installed. Candidate: RustDesk direct-IP over Tailscale, subject to a separate tailnet-only configuration and acceptance test. Windows 11 Home is not assumed to host built-in RDP.

## When access is lost

- Preserve existing Tailscale Serve routes; never use a broad Serve/Funnel reset.
- Do not change Tailscale identity, tags, grants, unattended mode, or key expiry from an unverified session.
- If the host agent or Gateway is down, use the separately accepted direct SSH path. Dashboard status is not a recovery prerequisite.
- If direct SSH is not accepted or fails, use local console or the accepted human fallback. Do not expose new public ports as an emergency workaround.
- If AI-Stack is offline and supervisor/job state cannot be read, do not stop or restart GPU workers. Preserve logs and ask for local operator review.

## Compromised client or key

After the replacement path is verified, revoke the affected tailnet node/client and remove its authorized SSH key from the approved account. Verify a rejected connection from that client, keep a sanitized timestamp/result, and rotate any related credential using the owning system. Never record key text or raw audit output in this runbook.

## Rollback

Rollback is not authorized by this preparation document. When a later apply is approved:

1. Confirm a separate management path works.
2. Read the protected `%ProgramData%\AI-Stack\RemoteAccess` manifest and restore only resources the feature changed.
3. Remove `rdc` task/rule before considering SSH rollback.
4. Restore SSH only from local console or a proven replacement path; do not remove the last working management route.
5. Compare current Serve/Funnel routes with the protected before-state and the sanitized design baseline. Preserve all routes, including 8446, 8448, and 8449.
6. Confirm the AI-Stack state and GPU leases before any stack lifecycle action.

## Acceptance evidence still required

R01–R19 and R22 from [the design record](../plans/remote-access-and-computer-control.md) require actual host and second-device results. No no-login reboot, wrong-key denial, unauthorized-device denial, WAN isolation, `rdc` audit, fallback recovery, revocation, or full rollback result is claimed yet.
