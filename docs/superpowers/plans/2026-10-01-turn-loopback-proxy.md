# Private TURN Loopback Ingress Proxy Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Provide the Tailscale TURN/TLS route with a loopback TCP listener while keeping coturn private and blocking proxy egress.

**Architecture:** Coturn stays on the existing internal voice network without host ports. A small TCP proxy sits on a dedicated publish bridge and the internal voice network, publishes only loopback TCP 3478, locks its own network namespace to default-deny rules, then drops all capabilities before accepting traffic.

**Tech Stack:** Docker Compose, Python 3.12 asyncio, iptables, util-linux setpriv, coturn 4.18.0, PowerShell 5.1.

**Spec:** 2026-10-01-turn-loopback-proxy-design.md

## Global Constraints

- Coturn has no host-published ports and remains on ai-stack-voice-net (internal: true).
- Only turn-proxy publishes 127.0.0.1:3478:3478/tcp; no host UDP ports are added.
- turn-proxy is the only service attached to turn-publish; it also joins the internal voice network.
- Proxy OUTPUT permits new TCP connections only to the validated coturn IPv4 address on port 3478.
- Proxy INPUT, OUTPUT, and FORWARD default to DROP; allow loopback, established/related traffic, and new TCP input on 3478.
- Before serving, the proxy process must have CapEff=0 and CapBnd=0; Docker retains no-new-privileges:true and read_only:true.
- The TURN shared secret is injected only into coturn and voice; the proxy receives only a non-secret enable flag.
- Port 8447 stays disabled until the host-agent loopback STUN check succeeds; Funnel remains off.
- Keep PowerShell scripts compatible with Windows PowerShell 5.1.

## Review Focus

- Missing/partial TURN environment must not leave a listening proxy or enable 8447; test in Task 1.
- Public, loopback, unspecified, multiple, or malformed upstream DNS answers must fail before firewall changes; test in Task 2.
- An iptables command failure must leave no listening socket; test in Task 2 and Task 3.
- Failure to drop effective or bounding capabilities must exit before listening; test in Task 2.
- An external TCP attempt after startup must fail while a STUN transaction through the proxy succeeds; test in Task 3.

---

## File Map

- voice/turn_proxy/proxy.py: upstream validation, firewall rule generation/application, capability drop, and asyncio TCP byte relay.
- voice/turn_proxy/Dockerfile: small Python runtime with the required firewall tooling.
- compose.yaml: coturn remains internal-only; add the dual-network proxy and private publish bridge.
- scripts/ai.ps1: start and stop coturn plus its proxy as one optional service pair.
- scripts/test-turn-proxy.ps1: local integration checks for listener, STUN forwarding, capability state, and blocked egress.
- voice/README.md: describe process-scoped config and the new private ingress path without credential values.
- tests/voice/test_turn_proxy.py and tests/voice/test_compose_turn.py: unit and Compose regression coverage.

## Task 1: Pin the Compose and lifecycle contract

**Files:** Modify compose.yaml, scripts/ai.ps1, and tests/voice/test_compose_turn.py.

- [ ] Step 1: Write failing Compose tests named test_coturn_has_no_published_ports, test_turn_proxy_publishes_only_loopback_tcp_and_uses_both_networks, test_turn_proxy_receives_no_shared_secret, and test_coturn_command_omits_removed_flags. Assert coturn has zero ports, proxy has only 127.0.0.1:3478:3478/tcp, proxy networks are ordered turn-publish then voice, and neither --no-dtls nor --no-cli appears.
- [ ] Step 2: Run python -m unittest tests.voice.test_compose_turn -v. Expected: the new Compose contract tests FAIL on the current direct coturn port mapping and flags.
- [ ] Step 3: Implement the service and lifecycle configuration. Add turn-publish as a normal bridge with a fixed project name; add turn-proxy with read_only, /tmp tmpfs, no-new-privileges, cap_drop: ALL, and temporary NET_ADMIN/SETPCAP. Publish only loopback TCP 3478. Do not pass either TURN secret or hostname to the proxy. Set TURN_PROXY_ENABLED only when both process-scoped TURN inputs are present and clear it after Compose returns. Update scripts/ai.ps1 to start/stop coturn and turn-proxy together.
- [ ] Step 4: Run python -m unittest tests.voice.test_compose_turn -v and docker compose config --quiet. Expected: all Compose contract tests PASS and Compose exits 0.
- [ ] Step 5: Commit as fix(voice): define private TURN ingress topology.

## Task 2: Implement the fail-closed TCP proxy

**Files:** Create voice/turn_proxy/__init__.py, voice/turn_proxy/proxy.py, voice/turn_proxy/Dockerfile, and tests/voice/test_turn_proxy.py.

**Interfaces:**

- resolve_upstream(host: str, port: int) -> IPv4Address accepts exactly one private IPv4 result.
- build_firewall_rules(upstream_ip: IPv4Address, *, listen_port: int = 3478, upstream_port: int = 3478) -> list[list[str]] returns the iptables argv calls in startup order.
- drop_privilege_argv(script_path: str) -> list[str] returns setpriv arguments clearing bounding, inheritable, ambient, and effective capabilities and setting no-new-privs.
- proxy_connection(client_reader: StreamReader, client_writer: StreamWriter, *, upstream_ip: str, upstream_port: int) -> None forwards the byte stream bidirectionally and closes both sockets after EOF/error.

- [ ] Step 1: Write failing unit tests test_resolve_upstream_accepts_one_private_ipv4, test_resolve_upstream_rejects_public_loopback_unspecified_and_ambiguous_answers, test_firewall_defaults_to_drop_and_allows_only_coturn_tcp, test_drop_privilege_command_clears_all_capability_sets, and test_proxy_connection_forwards_bytes_and_closes_on_upstream_failure. Assert no broad egress rule or DNS rule exists.
- [ ] Step 2: Run python -m unittest tests.voice.test_turn_proxy -v. Expected: FAIL because the proxy module does not exist.
- [ ] Step 3: Implement startup order: resolve coturn on the internal network; apply all firewall rules; exec through /usr/bin/setpriv; verify /proc/self/status has zero effective and bounding caps; only then bind TCP 3478. Any failure before bind exits nonzero.
- [ ] Step 4: Run python -m unittest tests.voice.test_turn_proxy -v. Expected: all proxy unit tests PASS.
- [ ] Step 5: Commit as feat(voice): add isolated TCP TURN ingress proxy.

## Task 3: Add reproducible local network acceptance

**Files:** Create scripts/test-turn-proxy.ps1; modify voice/README.md.

- [ ] Step 1: Write test test_turn_proxy_acceptance_script_checks_security_and_stun, asserting the script checks loopback STUN readiness, proxy PID capabilities, and an outbound internet TCP attempt without printing container environment or credentials.
- [ ] Step 2: Run the focused test and confirm it fails because the acceptance script is absent.
- [ ] Step 3: Implement the PowerShell 5.1 script. It must require the proxy container to be running, call existing host_agent.voice_turn_listener_ready(), check PID 1 CapEff/CapBnd, and confirm a new TCP connection to 1.1.1.1:443 is blocked. It must not start models, change Tailscale Serve, or read .env.
- [ ] Step 4: Run scripts/test-turn-proxy.ps1 with the deployed service. Expected: loopback STUN succeeds, both capability masks are zero, and egress reports blocked.
- [ ] Step 5: Commit as test(voice): add TURN proxy network acceptance.

## Task 4: Integrated verification and tailnet acceptance gate

**Files:** Modify only voice/README.md and the implementation files above if a verified failure requires a correction.

- [ ] Step 1: Run python -m pytest -q tests, python -m compileall -q voice gateway scripts tests, Dashboard Vitest/lint/build, PowerShell parse checks, docker compose config --quiet, and git diff --check. Expected: all exit 0.
- [ ] Step 2: Check supervisor status before lifecycle operations. Start coturn and turn-proxy with process-scoped random secret/hostname; verify host listener exactly 127.0.0.1:3478/tcp, no host UDP ports, STUN success, proxy egress block, and Tailscale Serve 8447/Funnel both off.
- [ ] Step 3: Enable the private 8447 Serve path only through the authenticated Dashboard host-agent UI after the listener checks pass; verify exact loopback target and Funnel off.
- [ ] Step 4: Run the actual second-tailnet-device WebRTC call and confirm selected relay candidate pair with TLS/TCP TURN transport, two-way audio, barge-in, reconnect/stop, and audible response.
- [ ] Step 5: Update PR #12 with the exact verification and call results. Do not merge unless the call and CI checks pass.

### Spike evidence (2026-10-01)

- Coturn inside the internal voice network accepted TCP on 3478.
- Direct host publication from the internal network did not create a Windows listener.
- A normal bridge did publish loopback TCP, but the proxy had internet egress before firewalling.
- A temporary proxy on a publish bridge plus the internal voice network returned a valid STUN response through coturn. Its firewall blocked a fresh connection to 1.1.1.1:443, and the serving process reported CapEff=0 and CapBnd=0.