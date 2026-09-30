# Realtime Voice Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an authenticated browser WebRTC voice test path through the existing voice runtime and gateway, with a Dashboard test page usable locally and by tailnet devices.

**Architecture:** Keep `RealtimeSession` and `RealtimeRuntime` responsible for conversation state, provider routing, and generation cancellation. Pipecat SmallWebRTC owns the peer and media frame boundary; a frame adapter converts to the existing 16 kHz mono PCM16 detector input, while a runtime audio sink sends provider output over the WebRTC media track. Gateway signaling, short-lived tickets, same-origin Dashboard calls, and an opt-in Tailscale Serve TURN route keep the current authentication and network boundaries intact.

**Tech Stack:** FastAPI, Pipecat `pipecat-ai[webrtc]==1.12.0`, NumPy/SciPy, React/Vite, `@pipecat-ai/client-js==1.13.1`, `@pipecat-ai/small-webrtc-transport==1.10.7`, Vitest, Docker Compose, coturn `4.18.0`, and the authenticated Windows host agent.

**Spec:** [2026-09-30-realtime-voice-phase2-design.md](../specs/2026-09-30-realtime-voice-phase2-design.md)

## Global Constraints

- Preserve the existing WebSocket v1 path and its event/audio behavior.
- `RealtimeRuntime` and `RealtimeSession` remain the canonical conversation state and cancellation owners.
- Browser requests use same-origin `/api` routes; `AI_API_KEY` stays server-side in Dashboard Nginx.
- A WebRTC offer is JSON `{ "type": "offer", "sdp": "..." }`, limited to 128 KiB, and authenticated with a one-use ticket in `X-Voice-Session-Ticket`.
- TURN credentials expire after 300 seconds; session ticket TTL remains the existing 60 seconds unless configured otherwise.
- Input conversion outputs mono PCM16 at 16,000 Hz; WebRTC output frames use mono PCM16 at 48,000 Hz.
- Metrics use fixed bounded labels only; no session, response, user, transcript, audio, IP, API key, or TURN credential values are labels or log fields.
- The TURN TCP listener binds to host loopback at port 3478; Tailscale Serve uses TLS-terminated TCP on port 8447; do not publish host UDP ports or enable Funnel for this route. Port 8446 already serves an unrelated endpoint.
- Remote access is for Tailscale-enrolled clients, including tailnet clients on the same physical LAN. Unenrolled LAN clients remain out of scope.
- Do not read, print, modify, or commit `.env` or `.env.*`; do not expose the Docker socket or change supervisor/GPU ownership.
- Keep repository scripts compatible with Windows PowerShell 5.1.

## Review Focus

- Empty, odd-byte, unsupported-rate, and unsupported-channel PCM input must fail as an invalid frame; pin this in `test_input_rejects_empty_odd_byte_malformed_and_unsupported_frames` (Task 1).
- A denied microphone or a browser that ignores processing constraints must show the actual permission/settings state without pretending AEC, noise suppression, or AGC is active; pin this in Task 6 UI tests.
- Concurrent/replayed offers must consume a ticket at most once and must not create a second peer; pin this in Task 4.
- A peer disconnect during active synthesis must cancel the current generation and close provider/transport resources once; pin this in Task 4.
- Missing TURN configuration or Tailscale CLI failure must leave the TURN route disabled, avoid Funnel, and avoid logging credentials; pin this in Tasks 3 and 7.

---

## File Map

- `voice/audio/frame_adapter.py`: pure PCM frame validation, channel conversion, resampling, and PCM16 conversion.
- `voice/runtime.py`: optional generation-guarded media sink; the default WebSocket event path remains unchanged.
- `voice/transports/small_webrtc.py`: Pipecat connection, transport, frame processor, control-message bridge, peer lifecycle, and idempotent close.
- `voice/transports/turn.py`: short-lived TURN REST credentials and public/internal ICE configuration.
- `voice/session.py`, `voice/app.py`, `voice/metrics.py`, `voice/requirements.txt`: session offer route, transport lifecycle, dependencies, and bounded WebRTC metrics.
- `gateway/app.py`: authenticated bounded SDP offer proxy and same-origin offer URL; existing metrics aggregation already includes voice `/metrics`.
- `dashboard/src/RealtimeVoicePanel.tsx`, `dashboard/src/realtimeVoice.ts`, `dashboard/src/api.ts`, `dashboard/src/App.tsx`, `dashboard/src/App.css`: small start/stop test panel, SDK wiring, session state, transcript/audio display, and error states.
- `dashboard/src/OpsPanels.tsx`: opt-in control and current status for the tailnet-only voice TURN route in the existing Network panel.
- `dashboard/package.json`, `dashboard/package-lock.json`, `dashboard/vite.config.ts`: pinned Pipecat client packages and focused UI test setup.
- `compose.yaml`: coturn service on the internal voice network with only its TCP listener bound to host loopback; optional TURN secret and hostname inputs fail closed when absent.
- `scripts/host_agent.py`: authenticated opt-in Tailscale Serve TLS-terminated TCP route and status.
- `tests/voice/`, `tests/gateway/test_realtime_voice.py`, `tests/host_agent/`, `dashboard/src/`: regression coverage for conversion, transport, auth, metrics, route safety, and Dashboard states.

## Task 1: PCM Frame Adapter

**Files:** Create `voice/audio/__init__.py`, `voice/audio/frame_adapter.py`, and `tests/voice/test_audio_frame_adapter.py`; modify `voice/requirements.txt`.

**Interfaces:**

- Produces `AudioPCMFrame(data: bytes, sample_rate: int, channels: int)`.
- Produces `convert_input_audio(data: bytes, *, sample_rate: int, channels: int) -> bytes`, returning mono PCM16 at 16,000 Hz.
- Produces `convert_output_audio(data: bytes, *, sample_rate: int, channels: int) -> AudioPCMFrame`, returning mono PCM16 at 48,000 Hz.
- Produces `float_to_pcm16(samples: numpy.ndarray) -> bytes`; use `scipy.signal.resample_poly` for rate conversion and `numpy.rint` plus signed-16-bit clipping for float conversion.
- Accept sample rates 16,000, 24,000, 32,000, 44,100, and 48,000 Hz; accept one or two channels. Reject empty input, odd PCM16 byte lengths, unsupported rates/channels, and malformed dimensions.

- [x] **Step 1: Write failing tests** for `test_input_downmixes_stereo_and_resamples_48khz_to_mono_16khz`, `test_input_accepts_supported_sample_rates_and_mono_or_stereo`, `test_input_rejects_empty_odd_byte_malformed_and_unsupported_frames`, `test_output_resamples_provider_pcm_to_48khz_mono_frame`, and `test_float_to_pcm16_clips_and_rounds_boundaries`. Assert exact output rate/channel metadata, expected frame lengths, mono downmix values, and clipping to `[-32768, 32767]`.
- [x] **Step 2: Run the focused tests.** Run `python -m unittest discover -s tests/voice -p "test_audio_frame_adapter.py" -v`. Expected: FAIL because `voice.audio.frame_adapter` is not implemented.
- [x] **Step 3: Implement** the three functions and dataclass in `voice/audio/frame_adapter.py`; add `numpy>=2,<3` and `scipy>=1.14,<2` to `voice/requirements.txt`.
- [x] **Step 4: Re-run the focused tests.** Expected: all five tests PASS.
- [x] **Step 5: Commit** as `feat(voice): add PCM frame conversion adapter`.

## Task 2: Generation-Guarded Runtime Audio Sink

**Files:** Modify `voice/runtime.py`; modify `tests/voice/test_runtime.py` and, if needed, `tests/voice/test_realtime_app.py`.

**Interfaces:**

- Add immutable `AudioOutput(pcm: bytes, sample_rate: int, channels: int, text: str, response_id: str, turn_id: str, generation_id: int)`.
- Extend `RealtimeRuntime.__init__(..., send_audio: Callable[[AudioOutput], Awaitable[bool]] | None = None)`.
- When `send_audio` is absent, keep emitting the current `response.audio.delta` base64 event. When present, call the sink under the same generation guard and account output bytes/text only when it returns `True`.

- [x] **Step 1: Write failing tests** `test_webrtc_audio_sink_replaces_base64_event_and_records_transport_acceptance`, `test_webrtc_audio_sink_rejects_stale_generation`, and `test_websocket_default_emits_existing_base64_audio_delta`. Assert the WebRTC path produces no base64 audio control event, a false/stale sink adds no emitted bytes/text, and the no-sink path keeps the current event fields and bytes.
- [x] **Step 2: Run** `python -m unittest discover -s tests/voice -p "test_runtime.py" -v`. Expected: the new sink tests FAIL while existing runtime tests remain green.
- [x] **Step 3: Implement** `AudioOutput` and the optional sink with generation checks serialized by the existing `_send_lock`; retain existing cancellation settlement and truncation accounting.
- [x] **Step 4: Run** `python -m unittest discover -s tests/voice -p "test_runtime.py" -v`. Expected: all runtime tests PASS, including the unchanged default path.
- [x] **Step 5: Commit** as `refactor(voice): support generation-guarded media output`.

## Task 3: TURN Credentials and Tailnet-Only Relay Route

**Files:** Create `voice/transports/turn.py`, `tests/voice/test_turn.py`, and `tests/host_agent/test_turn_route.py`; modify `compose.yaml` and `scripts/host_agent.py`.

**Interfaces:**

- Add `TurnCredentials(username: str, credential: str, expires_at: int)` and `make_turn_credentials(secret: str, *, now: int | None = None, ttl_seconds: int = 300, nonce: str | None = None) -> TurnCredentials` using coturn REST HMAC-SHA1 credentials with an expiring username.
- Add `client_ice_servers(credentials: TurnCredentials, *, hostname: str, port: int = 8447) -> list[dict[str, object]]`; the remote URL is `turns:<tailnet-host>:8447?transport=tcp`. If the secret or tailnet hostname is missing, return no TURN entries and keep the route disabled.
- Add an authenticated host-agent payload field `voice_turn_enabled: bool`; report the route as enabled only when `tailscale serve status --json` confirms the TLS-terminated TCP endpoint.

- [x] **Step 1: Write failing tests** `test_turn_rest_credentials_expire_after_five_minutes`, `test_turn_credentials_use_random_nonce_and_never_return_shared_secret`, `test_voice_turn_route_uses_tls_terminated_tcp_and_never_enables_funnel`, `test_disabling_voice_turn_removes_only_the_turn_route`, and `test_turn_route_stays_disabled_when_tls_terminated_tcp_is_unsupported`. Assert HMAC-derived password, `expires_at == now + 300`, no secret in serialized response, commands use `--tls-terminated-tcp=8447` to `tcp://127.0.0.1:3478` without an enabling Funnel command, the existing HTTPS route on 8446 is preserved, and CLI failure returns disabled status.
- [x] **Step 2: Run** `python -m unittest discover -s tests/voice -p "test_turn.py" -v` and `python -m unittest discover -s tests/host_agent -v`. Expected: FAIL because the credential helper and route option do not exist.
- [x] **Step 3: Implement** the credential helper; add coturn `4.18.0` on the internal `voice` network with TCP 3478 published only as `127.0.0.1:3478:3478/tcp`, and pass `VOICE_TURN_SHARED_SECRET` and `VOICE_TURN_HOSTNAME` only through Compose environment injection. Missing values must result in no TURN credentials and no route enablement. Do not publish UDP. Extend the host agent’s existing authenticated configure/status path with the opt-in `tailscale serve --tls-terminated-tcp=8447 --bg --yes tcp://127.0.0.1:3478` command and matching `off` command; preserve the unrelated HTTPS route on 8446.
- [x] **Step 4: Run** both focused unittest commands. Expected: all tests PASS. Run `docker compose config | Out-Null`; expected: exit code 0 without printing interpolated values.
- [x] **Step 5: Commit** as `feat(voice): add private TURN relay configuration`.

## Task 4: Pipecat WebRTC Peer and Voice Offer Endpoint

**Files:** Create `voice/transports/__init__.py`, `voice/transports/small_webrtc.py`, and `tests/voice/test_webrtc_transport.py`; modify `voice/session.py`, `voice/app.py`, `voice/requirements.txt`, `voice/metrics.py`, `tests/voice/test_realtime_app.py`, and `tests/voice/test_metrics.py`.

**Interfaces:**

- Add `SmallWebRTCPeer.accept_offer(offer: dict[str, str]) -> Awaitable[dict[str, str]]` and idempotent `SmallWebRTCPeer.close() -> Awaitable[None]`.
- Extend `create_app(..., webrtc_factory: Callable[..., SmallWebRTCPeer] | None = None)` for deterministic route tests.
- Add authenticated `POST /sessions/{session_id}/offer`, with the API key in `Authorization`, the one-use ticket in `X-Voice-Session-Ticket`, and a 128 KiB JSON limit. Require `type == "offer"` and a nonempty SDP string; consume the ticket before creating the peer.
- The session response adds `offer_url`, `ice_servers`, and the existing short-lived ticket; it retains `ws_url` for v1 callers. Store only the transient TURN credential required by the server peer in in-memory session state.
- Use `pipecat-ai[webrtc]==1.12.0`; build a `SmallWebRTCTransport` pipeline with audio input/output enabled, `audio_in_sample_rate=48000`, and `audio_out_sample_rate=48000`. A custom frame processor converts incoming audio with Task 1, feeds `VoiceTurnDetector`/`RealtimeRuntime`, converts `AudioOutput` to Pipecat `AudioRawFrame`, and forwards control JSON via the data channel.
- Keep the client control envelope `{ "protocol": "ai-stack.voice.v1", "type": ..., "session_id": ..., "turn_id": ..., "response_id": ..., "generation_id": ..., ... }`; audio PCM never goes on that channel.

- [x] **Step 1: Write failing tests** `test_offer_requires_api_key_and_one_use_ticket`, `test_replayed_and_concurrent_offers_create_at_most_one_peer`, `test_offer_rejects_invalid_type_and_sdp_over_128_kib`, `test_peer_disconnect_cancels_active_generation_and_closes_resources_once`, `test_audio_frames_feed_runtime_and_control_envelope_keeps_generation_identity`, and `test_webrtc_metrics_use_only_bounded_labels`. Assert invalid requests do not construct a peer, the second ticket use returns 401, disconnect calls runtime/provider/peer close once, and audio media does not become a base64 control message.
- [x] **Step 2: Run** `python -m unittest discover -s tests/voice -p "test_webrtc_transport.py" -v` and `python -m unittest discover -s tests/voice -p "test_realtime_app.py" -v`. Expected: new tests FAIL; existing WebSocket tests PASS.
- [x] **Step 3: Implement** the peer wrapper and route; pin `pipecat-ai[webrtc]==1.12.0`; add fixed-label offer, peer, frame/byte, conversion, failure-class, and drop counters/histograms to `VoiceMetrics`. Use lazy Pipecat imports in the app layer so existing WebSocket unit tests can inject a fake peer factory.
- [x] **Step 4: Run** the same focused commands plus `python -m unittest discover -s tests/voice -p "test_metrics.py" -v`. Expected: new and existing tests PASS; old WebSocket v1 event order remains unchanged.
- [x] **Step 5: Commit** as `feat(voice): add Pipecat SmallWebRTC sessions`.

## Task 5: Authenticated Gateway SDP Proxy

**Files:** Modify `gateway/app.py` and `tests/gateway/test_realtime_voice.py`.

**Interfaces:**

- The session response retains the current same-origin `ws_url` and adds `offer_url` as `/v1/realtime/sessions/{id}/offer`; never return an internal voice hostname to the browser.
- Add `POST /v1/realtime/sessions/{session_id}/offer`. Apply normal gateway API-key middleware, accept only JSON SDP offers up to 128 KiB, and forward them to `VOICE_URL` with the gateway bearer key and `X-Voice-Session-Ticket` header. Bound the upstream answer to 128 KiB and return only `{ "sdp": ..., "type": "answer" }`.

- [x] **Step 1: Write failing tests** `test_session_response_adds_same_origin_offer_url`, `test_offer_proxy_authenticates_bounds_and_forwards_ticket_out_of_url`, and `test_gateway_metrics_include_voice_webrtc_metrics`. Assert no ticket in URL, no internal voice hostname in response, oversize request rejection, and a bounded fake voice metric appears in `/metrics` output.
- [x] **Step 2: Run** `python -m unittest discover -s tests/gateway -p "test_realtime_voice.py" -v`. Expected: the new route assertions FAIL; existing WebSocket proxy tests PASS.
- [x] **Step 3: Implement** the route and response rewrite, using the existing `read_body_limited` helper and existing `/metrics` voice scrape.
- [x] **Step 4: Run** `python -m unittest discover -s tests/gateway -p "test_realtime_voice.py" -v`. Expected: all gateway realtime tests PASS.
- [x] **Step 5: Commit** as `feat(gateway): proxy authenticated WebRTC offers`.

## Task 6: Dashboard Voice Test Page

**Files:** Create `dashboard/src/RealtimeVoicePanel.tsx`, `dashboard/src/realtimeVoice.ts`, `dashboard/src/RealtimeVoicePanel.test.tsx`, and `dashboard/src/NetworkPanel.test.tsx`; modify `dashboard/src/App.tsx`, `dashboard/src/App.css`, `dashboard/src/OpsPanels.tsx`, `dashboard/src/api.ts`, `dashboard/package.json`, `dashboard/package-lock.json`, and `dashboard/vite.config.ts`.

**Interfaces:**

- Add `localAI.createRealtimeVoiceSession()` and return typed `id`, `offer_url`, `client_secret.value`, `ice_servers`, and `expires_at` fields.
- The panel creates a new session for every start/retry, connects `PipecatClient` with `SmallWebRTCTransport`, and passes the ticket in `webrtcRequestParams.headers`. It handles the `{protocol: "ai-stack.voice.v1", ...}` envelope through `onServerMessage`. All endpoints are relative same-origin `/api/...` URLs.
- Request microphone capture with `{ echoCancellation: true, noiseSuppression: true, autoGainControl: true }`; show actual `MediaTrack.getSettings()` values when available. Use the Pipecat transport media-manager hook to supply that captured stream and do not acquire a second track.
- Show idle, permission, connecting, connected, signaling failure, ICE timeout, disconnected, and stopped states; render user transcript and bot output events; stop closes the client and all local media tracks. Add a Network panel toggle for the tailnet-only voice TURN route, initially off. Do not silently fall back to WebSocket and do not include secrets/tickets in logs or URLs.
- Pin `@pipecat-ai/client-js==1.13.1` and `@pipecat-ai/small-webrtc-transport==1.10.7`; add Vitest/jsdom and Testing Library for component checks.

- [x] **Step 1: Write failing tests** `test_audio_constraints_request_processing_and_report_actual_settings`, `test_permission_denial_is_visible_and_does_not_create_session`, `test_signaling_failure_does_not_reuse_ticket_on_retry`, `test_connected_panel_shows_transcript_and_remote_audio`, `test_stop_disconnects_and_stops_microphone_tracks`, and `test_network_panel_exposes_voice_turn_opt_in_and_status`. Assert the three requested constraints, actual settings display (including unsupported/undefined), no session creation after permission denial, a fresh ticket after retry, complete cleanup, and a route toggle that defaults off and reflects host-agent status.
- [x] **Step 2: Run** `npm test -- --run src/RealtimeVoicePanel.test.tsx src/NetworkPanel.test.tsx` from `dashboard`. Expected: FAIL because the components and test setup are not implemented.
- [x] **Step 3: Implement** the panel, session API method, nav entry, transport media-manager adapter, callbacks, error states, and focused Vitest setup. Add a `test` script to `package.json` and commit the lockfile.
- [x] **Step 4: Run** `npm test -- --run src/RealtimeVoicePanel.test.tsx src/NetworkPanel.test.tsx`, `npm run lint`, and `npm run build` from `dashboard`. Expected: tests PASS, lint exits 0, Vite build completes.
- [x] **Step 5: Commit** as `feat(dashboard): add realtime voice test page`.

## Task 7: Integrated Verification and Remote Relay Gate

**Files:** Modify `voice/README.md` and `README.md` only for the short setup/test instructions; adjust implementation files only to fix verified failures.

**Interfaces:**

- Document local start/stop, same-origin signaling, the optional `VOICE_TURN_SHARED_SECRET` environment injection name, the opt-in host-agent `voice_turn_enabled` setting, and tailnet-only URL/port 8447. Note that the existing 8446 endpoint is preserved. Never include a credential value.
- A successful remote test requires a browser on a second tailnet device to use a selected relay candidate pair and complete two-way speech; local direct candidates do not satisfy the TURN check.

- [x] **Step 1: Run unit suites.** Run `python -m unittest discover -s tests/voice -v`, `python -m unittest discover -s tests/gateway -v`, and `python -m unittest discover -s tests/host_agent -v`. Expected: all tests PASS, including unchanged WebSocket v1 regressions.
- [x] **Step 2: Run static/build checks.** Run `python -m compileall -q voice gateway scripts/host_agent.py tests/voice tests/gateway tests/host_agent`, `npm test -- --run`, `npm run lint`, `npm run build` from `dashboard`, `docker compose config | Out-Null`, and `git diff --check`. Expected: all commands exit 0; Compose output is discarded so interpolated values are not printed.
- [ ] **Step 3: Run local integration.** Check current service/GPU lease status first; use `scripts/ai.ps1` for lifecycle changes and rebuild only the voice, coturn, gateway, and Dashboard services required by this feature. Verify local Dashboard start/stop, two-way speech, `/metrics`, no audio WebSocket, no gateway bypass, and no host UDP listener. Expected: local WebRTC succeeds and legacy WebSocket tests remain green.
- [ ] **Step 4: Run remote relay gate.** Confirm the installed Tailscale CLI accepts `--tls-terminated-tcp`, enable the authenticated host-agent route only after the loopback listener is healthy, and test from a second tailnet device. Inspect browser WebRTC stats to confirm a relay candidate pair, verify bidirectional speech and microphone settings, and confirm Funnel is off for port 8447. Expected: a tailnet relay call succeeds while the host exposes no UDP listener.
- [ ] **Step 5: Stop at the design gate if relay cannot work.** If the Docker Desktop/Windows path cannot form a TURN relay candidate pair without a host UDP mapping, leave the route disabled, do not publish an unrestricted UDP range, document the exact failure and observed listeners, and return for a revised network design before claiming Phase 2 complete.
- [x] **Step 6: Commit** documentation and any final fixes as `docs(voice): document Phase 2 WebRTC test path`.

### Verification record (2026-09-30)

- The local two-way Dashboard call was not run. The only live `ai-stack` Compose project is owned by the primary checkout at `D:\AI-Stack`; starting services from this worktree would replace its shared named containers, so the runtime was left unchanged.
- Tailscale CLI 1.102.2 supports `--tls-terminated-tcp`. Its existing private Serve route on port 8446 forwards to `127.0.0.1:8087`, so the voice route was moved to unused port 8447 and the existing route was covered by a regression test.
- No loopback listener exists on port 3478 and no TURN relay-range listeners were found. No second-device relay call was completed, so the Dashboard opt-in remains off and no relay candidate pair is claimed.
- Review follow-up adds the configured WebRTC session lifetime watchdog, Dashboard cleanup before transport-error retry, and a clean successful coturn exit when its optional configuration is missing.
- Automated checks passed in the final run: voice 48, gateway 29, host-agent 5, Dashboard 14; Python compileall, Dashboard lint/build, Compose config, PowerShell script parsing, and `git diff --check` also exited successfully. Compose emitted unset-variable warnings; no interpolated values were printed.

## References

- [Approved Phase 2 design](../specs/2026-09-30-realtime-voice-phase2-design.md)
- [Pipecat Small WebRTC server transport](https://docs.pipecat.ai/api-reference/server/services/transport/small-webrtc)
- [Pipecat Small WebRTC JavaScript transport](https://docs.pipecat.ai/api-reference/client/js/transports/small-webrtc)
- [Pipecat client methods and custom messages](https://docs.pipecat.ai/api-reference/client/js/client-methods)
- [Tailscale Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve)
- Version snapshot checked 2026-09-30: [Pipecat 1.12.0 on PyPI](https://pypi.org/project/pipecat-ai/), [Pipecat client-js 1.13.1](https://www.npmjs.com/package/@pipecat-ai/client-js), [Small WebRTC transport 1.10.7](https://www.npmjs.com/package/@pipecat-ai/small-webrtc-transport), [coturn releases](https://github.com/coturn/coturn/releases).
