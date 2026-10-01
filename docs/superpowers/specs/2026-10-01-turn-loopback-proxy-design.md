# Private TURN Loopback Ingress Redesign

**Status:** Proposed; awaiting user review
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

At startup, the proxy resolves the coturn service to one private IPv4 address before installing firewall rules. It rejects invalid, non-private, or ambiguous upstream addresses. It installs default-DROP INPUT, OUTPUT, and FORWARD policies in its own network namespace, then allows loopback, established/related traffic, new inbound TCP on 3478, and new outbound TCP only to the resolved coturn address on 3478. DNS resolution is not allowed after this setup.

The proxy container starts with only NET_ADMIN and SETPCAP added to its dropped capability set so it can install those namespace-local rules and clear its capability bounding set. It then uses setpriv with no-new-privs to exec the proxy. The serving process must verify both effective and bounding capabilities are zero before binding. The container remains read-only with a /tmp tmpfs and no-new-privileges.

scripts/ai.ps1 sets the non-secret TURN_PROXY_ENABLED flag only when both process-scoped TURN inputs are present, then removes that flag after Compose has created the services. Compose passes only this boolean to the proxy. Coturn and the voice service continue to receive the shared secret and hostname through the existing process environment injection. scripts/ai.ps1 start coturn starts coturn and the proxy; stop stops both. The host agent still controls Tailscale Serve and must verify the STUN listener before enabling 8447.

## Failure behavior

- Missing TURN configuration makes coturn and the proxy exit cleanly without an active TURN listener.
- Invalid coturn DNS results, firewall errors, or setpriv failures stop proxy startup before listen.
- An unavailable coturn upstream closes the individual client connection and does not change firewall policy.
- The private 8447 route stays off until the host-agent loopback STUN check succeeds. Funnel stays off.
- The normal bridge is not accepted as a standalone coturn network.

## Verification and acceptance

Automated verification must cover:

1. Compose publishes only proxy TCP 3478 to 127.0.0.1; coturn has no published ports; no UDP host mapping exists.
2. The proxy is attached to the dedicated publish bridge and internal voice network; its upstream is only coturn.
3. Firewall construction is default-deny and permits only the documented flows; invalid upstream addresses fail closed.
4. Firewall or privilege-drop failure prevents the listening socket from opening.
5. The running serving process reports CapEff=0 and CapBnd=0.
6. A real host STUN transaction through 127.0.0.1:3478 reaches coturn and succeeds.
7. A new outbound internet TCP connection from the proxy is blocked.
8. The full unit/static test matrix passes.

The deployment gate remains a real call from a second tailnet browser with a selected relay candidate pair, two-way audio, interruption, reconnect/stop behavior, and Funnel off. A successful local proxy probe is not a substitute.

## Alternatives considered

- Publish coturn from the internal network: fails on the target Docker Desktop host; not viable.
- Put coturn on a regular bridge: host port publication works, but outbound internet remains reachable; rejected.
- Run coturn natively on Windows/WSL: avoids Docker's internal-network port mapping, but changes runtime ownership and requires a separate, carefully constrained relay/firewall setup. Keep as fallback if the isolated proxy cannot pass all gates.
