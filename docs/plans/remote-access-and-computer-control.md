# Remote Access and Computer Control Design

**Status:** Implementation in progress; host provisioning is gated on operator decisions and second-device acceptance.
**Reviewed:** 2026-10-24
**Code baseline:** `ce3d4cf4b2661edc2f035f36731caaa31ce92751` (`ce3d4cf`)
**Source proposal:** `AI-Stack_Remote_Access_Implementation_Plan.md`

## Goal and order

Provide an independent Windows recovery path first, then signed-in desktop control, one tested human fallback, and sanitized status in AI-Stack. Boot recovery must not depend on Docker, Gateway, MCP, or the host agent. Desktop control is only available in the intended interactive Windows session; it does not bypass sign-in, lock screen, UAC, or secure desktop.

1. Confirm a least-privilege Windows account, Tailscale unattended behavior, existing network routes, firewall scope, and a safe SSH port.
2. Provision key-authenticated SSH only after those decisions; prove it from a second tailnet device after sign-out and reboot before adding desktop access.
3. Install pinned `rdc` at standard integrity for the signed-in user and validate its audit and recovery behavior.
4. Select and test one tailnet-only human fallback.
5. Add a read-only, authenticated readiness view to the existing host-agent → Gateway → Network-page path.

## AI-Stack boundaries

- Gateway remains the owner of API authentication, public routes, aliases, and proxying. The new status route is a read-only `/control/remote-access` proxy using existing Gateway and host-agent authentication.
- The current host agent listens on `0.0.0.0:8788`. Its `/health` is unauthenticated; other existing handlers require `X-Host-Agent-Token`. The new status handler must require that token and accept no request-supplied command, path, identity, or address. No listener or firewall exposure is added by the status feature.
- Supervisor retains Docker lifecycle and GPU exclusivity. The Docker socket remains exclusive to `docker-control`; no remote-access code or service receives it.
- Public MCP remains a narrow local-AI interface. Do not add shell, screenshot, mouse, keyboard, or arbitrary host operations to MCP or Funnel.
- SSH is the boot-time recovery plane. `rdc` is an interactive-session tool. Dashboard and MQTT status are optional observations and must never be prerequisites for SSH recovery.
- Tailscale Serve configuration is host-specific and must be preserved as found. Do not reset Serve/Funnel, change grants/tags/key expiry, or create router forwards as part of installation.

## Sanitized baseline

This is a read-only snapshot. It is not proof of off-site reachability or a completed recovery test.

| Area | Observation on 2026-10-24 | Decision / remaining evidence |
|---|---|---|
| Repository | Inventory-pinned source commit matches `HEAD` (`ce3d4cf`). The original `main` checkout had unrelated local changes; implementation is isolated on `codex/remote-access`. | Keep the original checkout and its changes untouched. |
| Windows | Windows 11 Home, build 26200. | Confirm after the eventual host install/reboot. |
| Tailscale | Service running; local CLI reports the host online. Unattended setting and node-key expiry were not established. | Verify in the supported Windows setting and tailnet admin view; do not change them automatically. |
| Existing tailnet routes | Read-only CLI reports tailnet-only routes on external ports 8443–8449 and 10000. Ports 8446, 8448, and 8449 are present in the live configuration although not listed in the inventory's static route table. | Preserve every observed route, including routes not named in the proposal. Recheck before and after any later change. The CLI did not report a public Funnel route; router forwarding and tailnet grants remain unverified. |
| OpenSSH / port 22 | Windows `sshd` service is absent. TCP 22 is already bound by `wslrelay`. | Do not install onto or displace the existing listener. The installer rejects port 22 while occupied; an alternate port still needs operator approval and live preflight. |
| Other remote-control ports | No listener was found on 7770, 21118, or 3389 at this snapshot. | This does not prove effective firewall or internet policy. |
| Firewall / router | All three Windows firewall profiles are enabled. A complete effective-rule and router-forward audit was not established. | Do not claim ports are closed from these observations. Inspect rule scope and router configuration before provisioning. |
| Windows account | The current interactive account belongs to Administrators and `docker-users`. | It is not approved for agent SSH. Choose a separate standard account and establish which app recovery operations it can perform without adding either group. |
| AI-Stack | `scripts\ai.ps1 status` showed the stack's containers stopped and the supervisor unavailable. | Active-job/lease state is unknown. No container lifecycle command has been run; do not start or stop services until an authoritative idle state is available. |
| Display / fallback | Windows 11 Home; monitor topology and human fallback behavior are not verified. No RustDesk viewer/service is attested by the status probe. | RustDesk direct-IP over Tailscale is selected; built-in RDP is not an available host option on this edition. Install/configure only after SSH passes its second-device and no-login acceptance. |
| Rollback | No feature-owned host resource exists. | No ProgramData rollback snapshot was created. Create a protected snapshot immediately before an approved apply, containing only resources the feature will change. |

The inventory is a useful code-level map at the exact base commit. Its explicit caveat that it does not verify live Tailscale, firewall, Docker, or model state is accurate. Live observations above override its static route list for preservation; they do not change the architecture boundaries.

## Operator gates before host changes

- Approve a Windows account that is not an administrator and not in `docker-users`, or revise the recovery scope if the required application operations cannot be done safely by such an account. Do not grant group membership to make the plan work.
- Resolve the current TCP 22 `wslrelay` listener before selecting a port. Do not stop WSL or take over port 22 automatically.
- Confirm Tailscale unattended mode, node-key expiry, the authorized client device and source addresses, and tailnet grants using the actual operator interfaces. Store private keys and identities outside Git.
- Audit effective Windows firewall rules for IPv4 and IPv6, local interface/address, profile, and remote source. Confirm router forwarding independently.
- Record the offline recovery sequence before fallback configuration: preserve the last working tailnet route; try accepted SSH; distinguish host/tailnet from dashboard failure; use the human viewer only after direct-IP tailnet access and its dedicated credential have been verified; use local console if both paths fail.
- Use RustDesk direct-IP over Tailscale as the single human graphical fallback. Keep its permanent password in the viewer's local credential store, disable public relay, restrict TCP 21118 to the tailnet, and verify the WAN path is blocked. Do not install the fallback until SSH is accepted from a second device.
- Any host change must have a dry-run, explicit apply, protected before-state, and a tested replacement management path before an existing rule is narrowed or removed.

## SSH implementation controls and current gates

The read-only PowerShell probes and SSH installer/rollback scripts are implemented, but no host resource has been changed. The installer defaults to a sanitized plan. Apply remains blocked until the operator selects a standard local account, supplies a public key and exact current Tailscale client IPs, approves an unused port, verifies host/tailnet policy and router exposure, and reviews the before-state plan. The current privileged account is rejected, and port 22 remains blocked while `wslrelay` is listening.

The SSH configuration binds to the host's current Tailscale addresses. Firewall rules are created separately for IPv4 and IPv6 and restricted to the corresponding local tailnet address, Tailscale interface, and exact client addresses. CIDR ranges and non-tailnet addresses are rejected. Password authentication is disabled before the new service starts; this avoids any password-authentication window, at the cost that an invalid key requires local-console recovery.

Windows capability installation can create a broad `OpenSSH-Server-In-TCP` rule. When that rule was absent before this feature and capability setup creates it, the installer disables only that exact feature-created rule before starting `sshd`. A pre-existing or otherwise broad effective rule blocks setup and is left unchanged. This is required because the current port-22 listener belongs to WSL; leaving the capability's default rule enabled could expose that listener. The manifest records ownership and the before-state under protected `%ProgramData%\AI-Stack\RemoteAccess`.

SSH rollback is inspect-only by default. Apply requires an explicit `-ConfirmManagementPath` attestation after local console or an independently verified path is available, and restores only manifest-owned resources. Snapshots remain available after rollback.

Existing OpenSSH host keys are never replaced. Keys generated to initialize a new OpenSSH service are recorded by name and SHA-256 and kept in the SYSTEM/Administrators-only `%ProgramData%\ssh` directory after feature rollback, preserving the server identity if the OS capability is installed again. This intentionally leaves those feature-created key files behind.

## Human fallback status

The selected fallback is RustDesk direct-IP over Tailscale. Its configuration and recovery behavior have not been installed or tested. The local readiness probe reports `unknown` until an operator records a protected, secret-free attestation at `%ProgramData%\AI-Stack\RemoteAccess\human-fallback-status.json`. A `ready` record is accepted only for `rustdesk-direct-ip` when it attests tailnet-only access, public relay disabled, a locally configured credential, blocked external access, a successful recovery after an `rdc` failure, and a test date within 90 days. The probe never reads the viewer's password or connection secret. Missing, malformed, stale, or incomplete records remain `unknown`; an explicit valid `disabled` record reports `disabled`.

## Remote status integration

The host agent exposes authenticated, GET-only `/remote-access/status`; it runs `scripts\remote\Test-RemoteReadiness.ps1 -Json` with a 12-second timeout and returns only normalized status fields. It does not accept a command, path, identity, or address from the request. A successful response is cached for 15 seconds. Gateway `GET /control/remote-access` uses the existing host-agent token and maps failures to 503; the local PowerShell readiness command remains independent of Gateway and Docker.

When optional MQTT publishing is enabled, `localai/marcus-computer/remote-access/status` carries only enumerated tailnet, SSH, desktop, and fallback states; bounded denial counts; host uptime/boot time; and check/audit timestamps. Missing host data publishes `unknown`. The additional summary is cached for 15 seconds and adds no MQTT subscriptions or command topics. The Network page displays boot recovery, interactive desktop, human fallback, and AI-Stack application health separately; a healthy Gateway snapshot never changes recovery or desktop status.

## Acceptance matrix

All are **not run** until performed on the real host with a second tailnet device. R20–R21 are optional/future and are not v1 gates.

| ID | Scenario | Required result |
|---|---|---|
| R01 | Authorized laptop on a different network | SSH accepts the authorized Ed25519 key. |
| R02 | Wrong SSH key | Login is denied. |
| R03 | Unauthorized Tailscale identity | SSH and `rdc` are blocked by effective policy. |
| R04 | WAN exposure | No direct public TCP 22, 7770, or selected fallback port. |
| R05 | Windows sign-out | SSH persists; `rdc` reports unavailable. |
| R06 | Reboot without sign-in | Tailscale and SSH return without an interactive logon. |
| R07 | Docker/Gateway down while safely idle | SSH still works; app-specific services may be offline. |
| R08 | Unprivileged project agent | Can inspect approved project files without host administration. |
| R09 | `rdc` capture/window list | Correct live desktop is visible while signed in. |
| R10 | `rdc` screenshot-click-screenshot | Intended UI action occurs and is audited. |
| R11 | View-only identity attempts a click | Click is denied and audited. |
| R12 | Lock, sign out, sign in | Availability transitions and recovery are correct. |
| R13 | Restart `rdc` in a test window | Task/process recovery is documented; SSH remains usable. |
| R14 | Temporary WAN outage | Routes recover after connectivity returns without a local fix. |
| R15 | Existing dashboard and MCP routes | 8443 and 10000 remain unchanged and retain their current authentication/exposure. |
| R16 | Existing voice TURN route | 8447 remains unchanged. |
| R17 | Human fallback with `rdc` unavailable | Human operator can regain desktop control. |
| R18 | Revoke a test node/key | Revoked access is denied. |
| R19 | Actual display/DPI modes | Pixel mapping works or a limit and workaround are documented. |
| R22 | Full rollback | Prior Tailscale, SSH, and app behavior is preserved. |

The actual Serve snapshot also contains 8446, 8448, and 8449; R15/R16 are not a complete route inventory. Those routes are also preservation requirements.

## Rollback checklist

1. Stop the feature operation and preserve the last working management path.
2. Read the protected feature manifest and confirm each resource is feature-owned before changing it.
3. Remove the `rdc` scheduled task and firewall rule only if the manifest shows the feature created them; leave SSH in place.
4. Restore SSH configuration/service/firewall state only from local console or a separately verified management path. Never remove the final working rule automatically.
5. Recheck every pre-existing Tailscale route and the AI-Stack state. Do not restore broad rules or overwrite custom SSH configuration blindly.
6. Revoke any test SSH key/client identity that should no longer have access, then remove protected snapshots only under the approved retention policy.
