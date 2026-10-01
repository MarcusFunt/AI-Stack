# Private TURN Loopback Ingress Redesign

**Status:** Implemented for PR #13; local automated acceptance complete; remote Tailnet/WebRTC acceptance is deferred until after merge
**Date:** 2026-10-01
**Supersedes:** The TURN ingress portion of 2026-09-30-realtime-voice-phase2-design.md for Windows Docker Desktop
**Reason:** The approved path assumes Docker Desktop can publish coturn from the private voice network to host loopback. That assumption failed on the target host.

## Observed behavior

On this Docker Desktop/Windows host:

- Coturn listens on TCP 3478 inside ai-stack-voice-net, which is internal.
- A container on that network with a 127.0.0.1:3478:3478/tcp publication has no effective host port mapping; Windows cannot connect to the container IP.
- Publishing the same listener from a normal bridge works, but that network has internet egress even with bridge IP masquerading disabled.
- The installed Tailscale host agent requires a healthy loopback TCP listener before it can safely configure the private Serve route on 8447.

The normal bridge workaround is rejected because it does not preserve egress isolation.

## Goals and constraints

- Keep coturn attached only to the existing internal voice network.
- Publish only TCP 3478 on Windows loopback, for the existing host-agent Serve path on tailnet port 8447.
- Publish no UDP port on the Windows host and never enable Funnel.
- Prevent the bridge-connected ingress component from opening new outbound connections except to coturn TCP 3478.
- Ensure the serving proxy process has CapEff=0 and CapBnd=0 before it accepts clients.
- Keep the TURN shared secret out of the proxy container and do not write secrets to .env files.
- Fail closed: if firewall setup, upstream validation, or capability dropping fails, the proxy must not bind 3478; the host-agent route remains disabled.

## Selected design

Coturn remains on ai-stack-voice-net and has no host-published ports. A small asyncio TCP byte-stream proxy attaches to two networks: a dedicated regular bridge used only for host-loopback port publication, followed by the internal voice network used to reach coturn. The proxy publishes 127.0.0.1:3478:3478/tcp; it does not parse or terminate TURN/TLS.

At startup, the proxy resolves the coturn service to one private IPv4 address before installing firewall rules. It rejects invalid, non-private, or ambiguous upstream addresses. It installs default-DROP INPUT, OUTPUT, and FORWARD policies in its own network namespace, then allows loopback, established/related traffic, new inbound TCP on 3478, and new outbound TCP only to the resolved coturn address on 3478. DNS resolution is not allowed after this setup. Before binding, it requires a valid TCP STUN Binding Success from coturn. Its healthcheck probes STUN through the loopback listener, and a watchdog exits after three consecutive upstream probe failures so Docker restarts the proxy and it resolves coturn again.

The proxy container starts with only NET_ADMIN and SETPCAP added to its dropped capability set so it can install those namespace-local rules and clear its capability bounding set. It then uses setpriv with no-new-privs to exec the proxy. The serving process must verify both effective and bounding capabilities are zero before binding. The container remains read-only with a /tmp tmpfs and no-new-privileges.

The coturn image remains based on coturn 4.18.0 and adds only a small Python TCP STUN readiness/recovery helper. Coturn has its own STUN healthcheck and watchdog; when STUN stays unavailable, the watchdog stops coturn and exits so Docker's `unless-stopped` policy recreates it. The proxy uses `unless-stopped` as well. Compose waits for healthy coturn, and an explicit Compose coturn restart also restarts the proxy. If coturn is recreated outside that Compose operation and receives a new IP, the proxy watchdog detects its pinned address is dead and exits; the restart resolves the service name again.

scripts/ai.ps1 sets the non-secret TURN_PROXY_ENABLED flag only when both process-scoped TURN inputs are present, then removes that flag after Compose has created the services. Compose passes only this boolean to the proxy. Coturn and the voice service continue to receive the shared secret and hostname through the existing process environment injection. scripts/ai.ps1 start coturn starts and builds only coturn and the proxy; stop stops both. The host agent still controls Tailscale Serve and must verify the STUN listener before enabling 8447.

## Failure behavior

- Missing TURN configuration makes coturn and the proxy exit cleanly without an active TURN listener.
- Invalid coturn DNS results, firewall errors, failed upstream STUN, or setpriv failures stop proxy startup before listen; Docker restarts failed containers unless an operator stopped them.
- Coturn's healthcheck requires a valid TCP STUN Binding Success. Coturn's watchdog exits nonzero after startup readiness never succeeds or three consecutive checks fail after readiness.
- The proxy's healthcheck requires STUN through the published listener. Its upstream watchdog exits after three consecutive failures, causing Docker to restart it and refresh coturn DNS.
- An unavailable coturn upstream closes the individual client connection. Sustained failure restarts coturn and/or the proxy through their watchdogs and restart policies.
- The private 8447 route stays off until the host-agent loopback STUN check succeeds. Funnel stays off.
- The normal bridge is not accepted as a standalone coturn network.

## Verification and acceptance

Automated verification must cover:

1. Compose publishes only proxy TCP 3478 to 127.0.0.1; coturn has no published ports; no UDP host mapping exists.
2. The proxy is attached to the dedicated publish bridge and internal voice network; its upstream is only coturn.
3. Firewall construction is default-deny and permits only the documented flows; invalid upstream addresses fail closed.
4. Firewall or privilege-drop failure prevents the listening socket from opening.
5. The running serving process reports CapEff=0 and CapBnd=0.
6. Coturn and proxy TCP STUN readiness checks, failure watchdogs, and restart policies pass automated failure-mode tests.
7. A real host STUN transaction through 127.0.0.1:3478 reaches coturn and succeeds.
8. A new outbound internet TCP connection from the proxy is blocked.
9. The PowerShell acceptance script executes its Python probes by script path and verifies host publication, network membership, health, STUN, capabilities, and blocked egress.
10. The full unit/static test matrix passes.

Remote Tailnet/WebRTC acceptance is intentionally deferred until after PR #13 is merged. The post-merge procedure is to enable only the private Tailscale Serve route on port 8447, verify Funnel remains disabled, connect from a second Tailnet device, confirm the browser selected a `relay` ICE candidate through TURN, then verify two-way audio, interruption, reconnect, and cleanup. This local implementation pass does not change Tailscale routes or perform that call.

## Alternatives considered

- Publish coturn from the internal network: fails on the target Docker Desktop host; not viable.
- Put coturn on a regular bridge: host port publication works, but outbound internet remains reachable; rejected.
- Run coturn natively on Windows/WSL: avoids Docker's internal-network port mapping, but changes runtime ownership and requires a separate, carefully constrained relay/firewall setup. Keep as fallback if the isolated proxy cannot pass all gates.
