# Realtime voice protocols

## WebSocket v1 compatibility protocol

The currently deployed transport remains `ai-stack.voice.v1` at `/ws/{session_id}`.
Create a session through the authenticated gateway endpoint
`POST /v1/realtime/sessions`, then connect with both `ai-stack.voice.v1` and the
one-use `ai-stack.ticket.<token>` subprotocols. The gateway continues to own
public authentication and proxies the socket to the voice service.

Audio input is mono, little-endian PCM16 at 16 kHz. Clients may send binary PCM
frames or JSON `input_audio_buffer.append` messages containing base64 PCM. JSON
control messages include `session.configure`, `input_audio_buffer.commit`,
`response.cancel`, and `session.close`.

The v1 server event names remain unchanged. They include session setup,
server-VAD speech boundaries, transcription deltas/completion, response
creation/text/audio deltas/completion or cancellation, truncation, errors, and
session close/timeout. Response events now also carry `session_id`, `turn_id`,
`response_id`, and a monotonically increasing `generation_id`; clients may
ignore these additive fields while newer clients use them to reject stale
output. A cancellation invalidates the old generation before provider work is
cancelled.

## Typed v2 contract foundation

`voice.protocol.RealtimeClientCommand` and `RealtimeServerEvent` describe the
planned v2 envelope. A v2 response event must carry `session_id`, `turn_id`,
`response_id`, and `generation_id`; `response.cancel` must name its target
response and generation. The current transport does not negotiate or emit v2
yet. This keeps the first runtime refactor compatible while giving future
WebRTC and other transports a validated event contract.

## Turn evaluation timing

Voice turn evaluation events retain the per-turn transcript, first-token, and
first-audio timings. They also include a bounded rolling baseline summary for
the current voice process: sample count and p50/p95 for each available timing.
The evaluation payload contains timing metadata only; it does not include
transcripts or audio content.

## Dashboard WebRTC test path

The authenticated browser test page is **Work → Realtime voice** in the
Dashboard. Start the local control plane with `scripts/ai.ps1 start voice` and
open `http://127.0.0.1:3000`; stop the voice service with
`scripts/ai.ps1 stop voice`. The page requests microphone access before it
creates a session, uses the gateway's same-origin `/api/v1/realtime/sessions`
and offer routes, and sends the one-use ticket in
`X-Voice-Session-Ticket`. It creates a new ticket for every retry and has no
WebSocket fallback. Stop the call to close WebRTC and release the local audio
track. The voice service also closes an otherwise-idle WebRTC session when it
reaches `VOICE_SESSION_MAX_SECONDS`.

Use the existing Dashboard HTTPS URL from a device enrolled in the Tailscale
network. This includes a tailnet device on the same physical LAN; being on the
LAN without Tailscale is not sufficient. The browser sends audio through
WebRTC, while the existing Dashboard Nginx route keeps the gateway API key on
the server.

### Optional tailnet TURN relay

TURN credentials are returned only when both `VOICE_TURN_SHARED_SECRET` and
`VOICE_TURN_HOSTNAME` are injected into the voice service environment. Never
put their values in browser configuration or logs. The host-agent
`voice_turn_enabled` setting defaults to false and is changed from the
Dashboard Network panel. It maps private Tailscale Serve TLS-terminated TCP
port 8447 to the coturn TCP listener on `127.0.0.1:3478`; Funnel and host UDP
port publishing are not part of this route.

Remote TURN is **not verified**. Port 8446 already serves an unrelated local
endpoint on `127.0.0.1:8087`; voice uses port 8447 to avoid changing that
route. The coturn command must be passed to its shell as one argument; otherwise
it exits before opening the listener. After that startup issue is fixed, the
current Compose service still publishes only loopback TCP port 3478. Coturn's
49160-49200 range is used for UDP relay endpoints, while Tailscale Serve only
forwards TCP. Port 8447 alone therefore cannot carry the relayed media path
from another tailnet device.

Keep `voice_turn_enabled` off until the relay data plane is redesigned and
tested without unrestricted host UDP publishing or Funnel. A successful
verification must use a second tailnet device, show a selected `relay`
candidate pair, and complete two-way audio; a direct candidate connection does
not count. See the upstream [coturn relay port options](https://github.com/coturn/coturn/wiki/turnserver)
and [Tailscale Serve TCP forwarding](https://tailscale.com/docs/reference/tailscale-cli/serve).
