# Remote Recovery Runbook

**Status:** Preparation only. Remote SSH, `rdc`, and a human fallback have not been configured or accepted on a second device.

## Before using this runbook

- Use only an operator-approved Windows account, SSH key, and tailnet client. Keep private keys, identities, source addresses, firewall exports, audit lines, and rollback snapshots outside Git.
- Direct SSH must remain usable when Docker, Gateway, MCP, the dashboard, or the host agent is unavailable.
- Do not use Tailscale Funnel, router forwarding, or public MCP for SSH or desktop access.
- The current Windows TCP 22 listener belongs to `wslrelay`; Windows `sshd` is absent. Do not stop WSL or change that listener. A safe port decision is still required.
- The current signed-in account belongs to Administrators and `docker-users`; it is not approved for agent SSH. A separate least-privilege account is unresolved.
- The stack was stopped during the baseline and supervisor status was unavailable. If GPU/job state is unknown, do not start, stop, rebuild, or restart services.

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
