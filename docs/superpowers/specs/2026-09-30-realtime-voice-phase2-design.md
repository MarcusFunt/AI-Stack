# Realtime Voice Phase 2: Pipecat and WebRTC

**Status:** Draft for user review
**Date:** 2026-09-30
**Basis:** `voice/REALTIME_VOICE_PLAN.md`, Phase 2
**Branch:** `codex/voice-realtime-phase2`

## Purpose

Add a browser voice path over WebRTC using Pipecat’s self-hosted SmallWebRTC transport, while keeping AI-Stack’s existing voice runtime, gateway, provider routing, and WebSocket v1 path. Add a small test page to the Dashboard. Remote test-page access is through the existing Tailscale HTTPS path; media must also work for a tailnet browser without exposing a new public UDP listener.

For this design, “LAN/Tailscale” means the local workstation and devices enrolled in the tailnet, including a tailnet device whose physical route is the same LAN. A device on the LAN without Tailscale is not included: current repository policy keeps network listeners loopback-only except through the Tailscale host-agent path. Supporting un-enrolled LAN devices would require a separately reviewed HTTPS and firewall exposure design.

## Goals

- Add Pipecat SmallWebRTC as a transport and media-frame layer; keep `RealtimeRuntime` and `RealtimeSession` as the canonical conversation state and cancellation owners.
- Add a minimal Dashboard voice test page with start, stop, microphone permission and connection state, transcript text, and audible assistant output.
- Route session creation and SDP signaling through the Dashboard’s same-origin `/api` proxy and authenticated gateway. Browser code must never receive `AI_API_KEY`.
- Keep WebSocket v1 behavior and tests compatible.
- Convert browser/WebRTC input to the existing mono PCM16 16 kHz turn detector input and convert provider output into the WebRTC output frame format.
- Request browser echo cancellation, noise suppression, and automatic gain control; report the settings the browser actually accepted where available.
- Add bounded, low-cardinality transport metrics for offer outcome, peer lifecycle, audio conversion, frame/byte counts, and dropped frames.
- Support Tailscale browsers through a TURN/TLS relay reached over the host-agent’s Tailscale TCP/TLS path, without publishing UDP ports on the host.

## Out of scope

- LiveKit, SIP/telephony, direct third-party realtime APIs, or Pipecat Cloud.
- Replacing the gateway, provider, invocation, or supervisor architecture.
- Semantic end-of-turn detection, Silero VAD, durable tasks, native speech-to-speech, and voice profiles; those belong to later plan phases.
- Direct access from devices that are not on the tailnet.

## Alternatives considered

1. **SmallWebRTC adapter in the existing voice service — selected.** Reuses the Phase 1 runtime and session model, keeps model calls on the existing gateway path, and preserves the current service boundary.
2. **Separate Pipecat voice sidecar.** Easier to follow standalone examples, but duplicates session lifecycle, authentication, and runtime wiring and risks creating a second control plane.
3. **Self-hosted LiveKit now.** Better suited to wider production deployments, but adds an SFU and operational footprint before the local WebRTC path has been validated. The plan places LiveKit after this phase.

## Components and ownership

| Component | Responsibility |
| --- | --- |
| Dashboard | Host the test page and call only same-origin `/api` routes. Use the Pipecat web client and SmallWebRTC client transport. |
| Nginx in Dashboard | Continue injecting the gateway bearer key server-side for `/api`; proxy signaling without returning the key to the browser. |
| Gateway | Authenticate HTTP calls; create sessions; proxy bounded SDP offer requests to voice; return only the short-lived session and ICE credentials needed by the client. |
| Voice service | Validate one-use session tickets, create the Pipecat WebRTC transport, bridge audio frames to `RealtimeRuntime`, and keep provider calls through the gateway. |
| TURN relay | Relay media for tailnet connections. Keep the service on internal Docker networks; publish only its TCP listener to host loopback for host-agent forwarding. Relay UDP allocations remain internal to the Docker network. |
| Host agent | Configure a Tailscale Serve TLS-terminated TCP endpoint for TURN, alongside the existing Dashboard route. Do not configure Funnel or a public listener. |

Pipecat owns peer transport and audio frame lifecycle only. AI-Stack continues to own authentication, model routing, conversation history, cancellation/generation identity, and provider policy. The browser and server use a small AI-Stack JSON control envelope over the Pipecat data channel; audio itself travels as WebRTC media, not base64 events.

## Session and media flow

1. The browser loads the Dashboard locally or through its existing Tailscale HTTPS URL. Starting a call requests microphone access with `echoCancellation`, `noiseSuppression`, and `autoGainControl` enabled, then records the values returned by `MediaTrack.getSettings()` when supported.
2. The page posts to the existing same-origin `/api/v1/realtime/sessions`. Nginx injects `AI_API_KEY`; the gateway authenticates and asks voice to create a bounded session. The response contains a session ID, a one-use short-TTL session ticket, the offer endpoint, and ICE server configuration with short-lived TURN credentials. It contains no long-lived API key.
3. The Pipecat client gathers the browser offer and posts it to a same-origin gateway signaling route, passing the one-use ticket separately from the gateway bearer credential. The gateway validates its normal API key and forwards only the bounded signaling request and ticket to voice. Voice consumes the ticket once and binds the peer to the session. A failed first offer requires a new session rather than reusing a consumed ticket.
4. Pipecat’s `SmallWebRTCTransport` receives decoded input audio frames. A dedicated frame adapter validates frame format, downmixes to mono, resamples to 16 kHz, and converts to little-endian PCM16 before feeding `VoiceTurnDetector` and `RealtimeRuntime`.
5. `RealtimeRuntime` continues to use the current cascaded provider through the gateway. Text, turn, cancellation, completion, and error events are adapted onto the WebRTC data channel with the existing session/turn/response/generation identity fields. Provider PCM output goes to the WebRTC audio track; the existing WebSocket v1 adapter continues to send its current audio event format.
6. The output adapter converts provider PCM rate/channel layout into the configured Pipecat output frame format. Conversion and transport acceptance are measured separately. In Phase 2, “emitted” audio means audio accepted by the server transport; browser playback acknowledgement/cursor accounting remains Phase 4 work.
7. On browser stop, peer failure, or session timeout, cancel the active generation, close Pipecat and provider resources, release the session, and update peer/session metrics. Stale generation output remains rejected by the Phase 1 runtime guards.

## Remote network path

SmallWebRTC is peer-to-peer and needs reachable ICE candidates. The current Tailscale Serve path provides Dashboard HTTPS but not UDP forwarding on this Windows host. Tailscale documents TCP forwarding and TLS-terminated TCP in Serve; its UDP layer-3 service path is Linux-only. The design therefore uses a relay for remote tailnet clients:

- Run a private TURN service on the voice Docker network.
- Bind its TURN TCP listener to host loopback only.
- Add an authenticated host-agent option that configures a Tailscale Serve TLS-terminated TCP port for that loopback listener. Use the tailnet HTTPS certificate at the edge and forward TURN TCP to the private relay. Keep the route tailnet-only and never expose it through Funnel.
- Issue expiring TURN credentials for each session. The shared secret is supplied through the existing secret-injection mechanism; it is not written into repository `.env` files, source, browser assets, or logs.
- Keep TURN relay allocation ports inside Docker. Both the client and voice peer use TURN relay candidates for remote tailnet calls. Local direct ICE may be used when it succeeds; the relay remains available as fallback.

A focused integration check must confirm that the installed Tailscale client supports the required TLS-terminated TCP Serve command and that a browser on another tailnet device can establish a relay candidate through it. If the TURN relay cannot form a working candidate pair in the current Docker Desktop/Windows environment, stop before enabling remote access and revise this design; do not compensate by opening an unrestricted UDP range.

## Signaling, limits, and failure behavior

- Preserve the existing gateway API-key middleware and Dashboard Nginx secret injection.
- The offer endpoint accepts only a supported content type, a bounded SDP body, a valid session ID, and the one-use session ticket. It cannot select arbitrary upstream URLs or providers.
- Do not log API keys, TURN secrets, TURN credentials, session tickets, complete SDP, transcripts, or audio.
- Expire TURN credentials quickly, bound active peer/session counts, and close orphaned peers after disconnect or timeout.
- Surface distinct UI states for microphone denial, session creation failure, signaling failure, ICE timeout, peer disconnect, and normal stop.
- Failure to establish WebRTC must not silently fall back to WebSocket; the test page should state the failure and leave the existing WebSocket client/path available separately.

## Metrics

Expose fixed-cardinality Prometheus metrics through the existing voice `/metrics` endpoint and gateway metric aggregation:

- WebRTC offers started, succeeded, and failed, with bounded failure classes.
- Active peers and total peer disconnects by bounded terminal class.
- Input/output audio frames and bytes.
- Conversion time and failed/unsupported frame count.
- Dropped or late frames, if Pipecat exposes them.
- Negotiated codec/sample-rate/channel information using a bounded enum or info metric, never session IDs as labels.

Metric buffers and labels must remain bounded. Do not add user, session, response, transcript, or IP address labels.

## Verification and acceptance

Phase 2 is complete when all of the following are demonstrated:

1. A tailnet browser loads the Dashboard test page over the existing HTTPS route and completes a two-way browser conversation over WebRTC, without the legacy audio WebSocket carrying media.
2. The test confirms the browser requested AEC/noise suppression and reports the effective settings the browser exposes.
3. The session uses existing gateway-routed STT, LLM, and TTS; no model call bypasses the gateway.
4. Input and output format conversion has unit tests for supported rates/channels, invalid frames, and clipping/rounding boundaries.
5. Ticket reuse, invalid ticket, oversize SDP, provider failure, peer disconnect, and stale generation output have tests.
6. WebRTC metrics appear through `/metrics`; no metric label has a per-session value.
7. Tailscale TURN/TLS works from a second tailnet device. No host UDP listener is published, the new TCP/TLS route is managed through the authenticated host agent, and the route is not public.
8. Existing WebSocket v1 regression tests pass unchanged, along with the relevant voice, gateway, and Dashboard checks.

## Implementation boundary

Expected implementation areas are `voice/transports/`, `voice/audio/`, `voice/runtime.py`, `voice/app.py`, `voice/metrics.py`, `gateway/app.py`, `compose.yaml`, the Dashboard client and Nginx proxy, and the existing host-agent Tailscale configuration path. Keep changes limited to Phase 2 and its required remote media path. Do not include Phase 3+ features or unrelated dashboard work.

## References

- [AI-Stack realtime voice plan](../../../voice/REALTIME_VOICE_PLAN.md), sections 7, 8, 18, and 26 Phase 2.
- [Pipecat Small WebRTC Transport](https://docs.pipecat.ai/api-reference/server/services/transport/small-webrtc).
- [Pipecat transport choice](https://docs.pipecat.ai/client/concepts/choosing-a-transport).
- [Tailscale Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve).
- [Tailscale Services transport limitations](https://tailscale.com/kb/1552/tailscale-services).