# Private TURN Loopback Ingress Proxy Implementation Plan

**Status:** PR #13 implementation and local verification complete; remote Tailnet/WebRTC acceptance is post-merge.

**Goal:** Provide the private Tailscale TURN/TLS route through a host-loopback TCP listener while keeping coturn private and blocking proxy egress.

**Architecture:** Coturn stays on the internal voice network with no host port. A TCP proxy joins only the publish bridge and voice network, publishes TCP 3478 on loopback, applies default-deny firewall rules, drops all capabilities before listening, and restarts with a fresh coturn DNS lookup after sustained upstream STUN failure.

**Tech Stack:** Docker Compose, Python 3.12 asyncio, iptables, util-linux setpriv, coturn 4.18.0, PowerShell 5.1.

**Spec:** [2026-10-01-turn-loopback-proxy-design.md](../specs/2026-10-01-turn-loopback-proxy-design.md)

## Completed implementation

- [x] Coturn is only on `ai-stack-voice-net`; it has no host-published ports.
- [x] Turn proxy alone joins `ai-stack-turn-publish-net` and `ai-stack-voice-net`; it publishes only `127.0.0.1:3478:3478/tcp`.
- [x] Proxy receives only `TURN_PROXY_ENABLED`, never the TURN shared secret or hostname.
- [x] Proxy firewall defaults INPUT, OUTPUT, and FORWARD to DROP; new egress is only TCP/3478 to one validated private coturn address.
- [x] Proxy has `read_only`, `/tmp` tmpfs, `no-new-privileges`, `cap_drop: ALL`, and temporary `NET_ADMIN`/`SETPCAP`; serving PID 1 must have `CapEff=0` and `CapBnd=0`.
- [x] Coturn remains pinned to 4.18.0. Its small derived image adds a TCP STUN healthcheck and watchdog; sustained failure exits so `unless-stopped` can restart it.
- [x] Proxy healthcheck requires a successful STUN transaction through its local listener. Startup probes coturn before binding; after three consecutive upstream failures, the process exits and Docker restarts it to re-resolve coturn.
- [x] Compose waits for coturn health and restarts the proxy on an explicit coturn Compose restart. Container recreation outside Compose is handled by the proxy watchdog and fresh DNS lookup.
- [x] `scripts/ai.ps1 start coturn` builds and starts only coturn and turn-proxy; stop handles the same pair.
- [x] `scripts/test-turn-proxy.ps1` uses a checked-in Python probe by script path and verifies health, exact network membership, loopback-only publication, host STUN, zero capability masks, and blocked external TCP.
- [x] Regression tests execute the PowerShell-to-Python handoff and cover upstream loss, changed coturn addresses, proxy startup, firewall, privilege drop, host-agent fail-closed state, and topology.

## Final local verification

- [x] Run voice (79 tests), host-agent (18 tests), and architecture unit suites (core, observability, gateway, supervisor, eval-router, MCP).
- [x] Run Dashboard Vitest (22 tests), lint, and production build.
- [x] Run STT benchmark tests (86 tests) in an isolated environment with the CI dependency set and `python -m compileall -q voice gateway scripts tests`.
- [x] Parse all 18 PowerShell scripts, run Compose config validation with an explicit empty interpolation file, and run `git diff --check`.
- [x] Confirm the supervisor had no active jobs, rebuild only coturn and turn-proxy, and run `scripts/test-turn-proxy.ps1` against those local containers.

## Post-merge Tailnet/WebRTC acceptance

- [x] Document that this implementation pass does not enable Tailscale Serve, Funnel, or a manual Tailnet route.
- [x] Document the post-merge acceptance procedure below.
- [x] After PR #13 is merged, enable only the private Tailscale Serve route on port `8447` through the authenticated host-agent setting and verify Funnel remains disabled.
- [ ] Connect from a second Tailnet device and confirm the selected browser ICE candidate is `relay` through TURN.
- [ ] Verify two-way audio, interruption/barge-in, reconnect, stop, and cleanup; then disable the route if no longer needed.

**Current acceptance status (2026-10-04):** the `8447` route is already Tailnet-only with Funnel disabled, and `scripts/test-turn-proxy.ps1` passes its local checks. The host currently has zero online Tailnet peers, so remote relay selection and call lifecycle checks remain outstanding. No route changes were made during this verification.

The local acceptance script does not enable or modify Tailnet routes. A direct ICE path does not satisfy remote relay acceptance.
