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
