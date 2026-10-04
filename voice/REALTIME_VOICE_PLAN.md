# Realtime Voice and Call Architecture Plan

**Status:** active roadmap; refreshed 2026-10-04

**Scope:** `voice/`, gateway integration, tool broker, observability, evals, dashboard, and later telephony  
**Primary goal:** evolve the existing AI-Stack realtime voice service into a low-latency, interruptible, model-independent conversational runtime that can serve browser, desktop, robot/device, and telephone clients without creating separate agent implementations.

## Current status and priority

The current branch has shipped the WebSocket v1 runtime, a transport-independent `RealtimeSession` and `RealtimeRuntime`, typed v2 event/command schemas, generation-safe interruption, and a Pipecat SmallWebRTC path exposed by the Dashboard. The v1 WebSocket remains the compatibility transport; v2 is a schema foundation and is not negotiated on the wire. The current branch also contains the speech API/UI, STT format/timestamp, and CustomVoice TTS startup changes being settled alongside this roadmap refresh.

Local TURN proxy acceptance passes. The private Tailnet Serve route on port 8447 is configured with Funnel disabled. Remote TURN acceptance is still open: the host has no online Tailnet peer at the moment, so a second-device call, selected `relay` ICE pair, two-way audio, interruption, reconnect, and cleanup have not been verified. Do not count a direct ICE path as completion; see the [TURN acceptance plan](../docs/superpowers/plans/2026-10-01-turn-loopback-proxy.md).

After the current branch changes are settled, use this order:

1. Complete remote TURN acceptance from a second Tailnet device.
2. Improve turn-taking quality with hesitation/non-terminal-pause fixtures and a hybrid end-of-turn decision.
3. Add client playback acknowledgement and measure interruption-to-silence latency.
4. Add durable background task ownership across disconnect and reconnect.
5. Expand evaluation to score end-of-turn, interruption, playback, and task lifecycle behavior.
6. Revisit voice profile scheduling, native speech providers, and LiveKit/SIP after those core behaviors have evidence.

The first five items are the next product milestones. Keep current one-worker GPU scheduling and the gateway/supervisor ownership boundaries intact while implementing them.

## 1. Current baseline on this branch

AI-Stack is not starting from zero. Preserve the working behavior unless a phase below explicitly replaces it.

Current `voice/` implementation already provides:

- authenticated `POST /sessions` creation with bounded, one-use WebSocket tickets;
- a `VoiceSessionRegistry` with bounded session count and expiry;
- English-only realtime sessions;
- PCM16 16 kHz input over WebSocket;
- server-side energy VAD with silence hangover and bounded turn buffers;
- barge-in: new user speech cancels an active assistant response;
- gateway-only STT, LLM, and TTS access through `GatewayVoiceProviders`;
- streamed LLM deltas and sentence-sized TTS chunking;
- response truncation bookkeeping so interrupted assistant text reflects audio that was actually emitted;
- OpenTelemetry/OpenInference spans for turn, STT, LLM, and TTS work;
- Prometheus-style voice metrics;
- asynchronous reporting to `/internal/voice-evaluations`;
- unit/integration tests covering sessions, turn detection, metrics, and the realtime app.

These are the current foundations. Most of the first internal refactor is now present; use the phase checklists below to distinguish implemented seams from the remaining acceptance work.

## 2. Target system

Realtime voice should be a first-class execution surface over the same canonical AI-Stack invocation/tool architecture used by text APIs.

```text
User surfaces
  Browser / Desktop / Phone / Robot / Embedded client
                         |
               WebRTC / SIP / local audio
                         |
                Realtime media gateway
                         |
                  Voice runtime
     VAD / turn-taking / interruption / session state
             /                         \
      Cascaded provider           Native S2S provider
   STT -> Invocation -> LLM         audio <-> model
              |                          |
         Tool Broker / durable task runtime
              |
       MCP / MQTT / HA / internal tools
              |
       foreground + background agents
```

The client must not care whether a response was produced by a cascaded STT+LLM+TTS path or a native speech-to-speech model.

## 3. Architectural invariants

1. The existing gateway remains the public authentication/model-routing boundary.
2. The supervisor remains the only component allowed to own heavyweight model lifecycle and GPU exclusivity.
3. Voice providers invoke models through the gateway or an explicitly defined realtime provider adapter; they must not bypass the supervisor contract.
4. Realtime voice owns conversational session/media semantics, not Docker lifecycle.
5. Tool execution goes through the canonical Tool Broker / Invocation model rather than creating a voice-specific tool API.
6. Browser, desktop, robot, and SIP clients converge on the same `RealtimeSession`.
7. Long-running tool/agent work survives media disconnects.
8. A native S2S provider must never make raw transcript text the sole canonical record of an audio turn.
9. Stale audio, model output, or tool results must never be emitted after cancellation/replacement.
10. English-only is acceptable for the first realtime milestone; language expansion is a separate capability decision.

## 4. Split the current service into explicit layers

The existing `voice/app.py` currently owns transport, session lifecycle, VAD, provider calls, response streaming, interruption handling, tracing, and event emission. Keep behavior but separate responsibilities before adding more transports.

Recommended logical modules:

```text
voice/
  app.py                   # FastAPI composition and health/readiness only
  protocol.py              # event/command schemas and versioning
  session.py               # RealtimeSession + registry/state
  runtime.py               # conversation state machine
  transports/
    websocket.py           # compatibility transport
    webrtc.py              # primary interactive transport
    livekit.py             # later production transport
    sip.py                 # later telephony adapter
  audio/
    vad.py
    turn_detection.py
    playback.py
    framing.py
  providers/
    base.py
    cascaded.py
    native_realtime.py
    qwen_reference.py
  tasks.py                 # durable background-task integration
  metrics.py
```

Do not require this exact file split in one PR. The requirement is clear boundaries and testable interfaces.

## 5. Canonical RealtimeSession

Extend the current `VoiceSession` into a transport-independent `RealtimeSession`.

Minimum state:

- `session_id`
- authenticated principal
- trace context
- conversation ID
- selected voice profile
- transport type
- provider type
- input/output audio formats
- session state
- user/assistant turn IDs
- active response generation ID
- active playback generation ID
- active foreground tool calls
- durable background task IDs
- reconnect metadata
- rolling conversational context reference
- total input/output audio counters

State machine:

```text
IDLE
 -> LISTENING
 -> USER_SPEAKING
 -> TURN_PENDING
 -> TRANSCRIBING        # cascaded only
 -> THINKING
 -> TOOL_RUNNING        # foreground tool
 -> SPEAKING
 -> LISTENING

Any active response state
 -> INTERRUPTED
 -> LISTENING

Any connected state
 -> RECONNECTING
 -> previous logical session state

Any state
 -> CLOSED / ERROR
```

`USER_SPEAKING` and `SPEAKING` may overlap briefly while barge-in is being detected. That overlap must not create two valid assistant generations.

## 6. Realtime protocol v2

Keep v1 compatibility until the new client exists, but introduce a documented, versioned event protocol.

Server events:

```text
session.created
session.updated
session.reconnecting
session.closed

input_audio.started
input_audio.level
input_audio.ended

user.speech_started
user.speech_stopped
user.transcript.delta
user.transcript.final

turn.created
turn.committed

response.created
response.text.delta
response.audio.delta
response.interrupted
response.completed

tool.call.started
tool.call.completed
tool.call.failed

task.accepted
task.progress
task.completed
task.failed
task.cancelled

conversation.summary.updated
error
```

Client commands:

```text
session.update
input_audio.enable
input_audio.disable
input_audio.commit

response.request
response.cancel

task.status
task.modify
task.cancel

conversation.message.create
conversation.context.add

voice.profile.set
session.close
```

Every event that can race must carry enough identity to reject stale work:

- `session_id`
- `turn_id` when applicable
- `response_id`
- monotonically increasing `generation_id`
- `task_id` for durable work

Do not infer cancellation correctness from task completion order.

## 7. Replace WebSocket audio as the primary media path

Keep the current WebSocket protocol as a compatibility and test transport.

Add WebRTC as the preferred interactive media path because it provides:

- jitter buffering;
- RTP media timing;
- packet-loss handling;
- browser-native acoustic echo cancellation;
- browser-native noise suppression;
- congestion adaptation;
- separate media and control semantics.

### Phase A transport

Use Pipecat as the media/pipeline runtime with a small/self-hosted WebRTC transport for LAN/Tailscale testing.

### Phase B transport

Add self-hosted LiveKit as the durable media substrate once the local WebRTC path is stable.

The voice state machine and provider API must not depend on a specific transport.

## 8. Audio pipeline

Target browser pipeline:

```text
microphone
 -> browser AEC/noise suppression
 -> WebRTC
 -> decoded PCM frames
 -> VAD
 -> turn detector
 -> provider pipeline
 -> cancellable audio queue
 -> WebRTC playback
```

Initial server internal format can remain mono PCM16.

Do not force every provider to use 16 kHz. Introduce conversion at provider boundaries so native 24/48 kHz realtime models and TTS engines do not distort the core protocol.

Track:

- input sample rate;
- output sample rate;
- channels;
- codec at transport edge;
- resampling latency;
- dropped/late frames;
- jitter if supplied by the transport.

## 9. VAD and semantic turn detection

The current RMS threshold VAD is useful as a deterministic fallback but must stop being the final turn detector.

### Stage 1: speech activity

Adopt Silero VAD or equivalent lightweight local VAD.

Output should include:

- speech start timestamp;
- speech probability;
- speech end candidate;
- frame-level confidence.

### Stage 2: end-of-turn

Implement a hybrid end-of-turn policy using:

- VAD silence duration;
- streaming transcript state;
- punctuation/decoder evidence;
- semantic end-of-turn score;
- maximum wait;
- user profile thresholds.

Example:

```text
if strong semantic EOT and >= short_silence:
    commit
elif weak/unknown semantic EOT and >= normal_silence:
    commit
elif >= hard_max_silence:
    commit
else:
    keep listening
```

A short hesitation in:

> "I want you to ... um ... check the repository"

must not automatically commit the turn.

Retain manual `input_audio.commit` for deterministic clients and testing.

## 10. Barge-in and cancellation

Interruption handling is a core quality feature.

On confirmed user speech while the assistant is speaking:

1. increment generation ID;
2. stop client/server playback queue immediately;
3. cancel further TTS chunk generation for the old generation;
4. cancel or detach old LLM generation;
5. mark the response interrupted;
6. preserve only assistant content that was actually audible;
7. begin buffering the new user turn;
8. ignore all late deltas carrying the old generation ID.

Track:

- `assistant_audio_cursor_ms`
- `speech_started_at`
- `barge_in_detected_at`
- `playback_stopped_at`
- `generated_text`
- `audible_text`
- `generation_id`

Acceptance target: median confirmed barge-in -> silence below 120 ms locally; initial acceptable ceiling 250 ms.

The current `visible_assistant_text()` behavior is worth preserving, but it should eventually use playback acknowledgement/cursor information rather than assuming all emitted bytes were heard.

## 11. Provider abstraction

Create one high-level realtime provider contract.

Conceptually:

```python
class RealtimeProvider:
    async def start_session(...)
    async def push_audio(...)
    async def commit_turn(...)
    async def cancel_response(...)
    async def update_context(...)
    async def events(...) -> AsyncIterator[RealtimeEvent]
    async def close(...)
```

Implement two families.

### CascadedRealtimeProvider

```text
audio
 -> streaming/batched STT
 -> canonical Invocation
 -> streaming LLM
 -> phrase chunker
 -> streaming TTS
 -> audio
```

The current `GatewayVoiceProviders` becomes the first cascaded implementation.

### NativeRealtimeProvider

```text
audio
 -> native duplex speech model
 -> text/tool events as available
 -> audio
```

Both providers normalize into the same protocol events.

Native providers may expose transcript text for display/search, but preserve original audio and provider-native event metadata.

## 12. Voice profiles

Move model/runtime policy into declarative profiles.

Recommended profiles:

```text
local-fast
local-quality
offline
cloud-reference
phone
```

A profile should declare:

- transport preferences;
- input/output audio capabilities;
- VAD settings;
- semantic turn detector;
- STT alias;
- LLM/reasoning alias;
- TTS alias/voice;
- optional native S2S provider;
- tool permissions;
- max session/context limits;
- target latency class;
- resource/VRAM budget.

Profiles must use gateway model aliases rather than hard-coding implementation model names into the voice runtime.

## 13. GPU/resource policy

Realtime voice is different from ordinary one-model-at-a-time AI-Stack use because a cascaded call may need STT, LLM, and TTS with conversational latency.

Do not weaken the supervisor invariant.

Instead:

1. declare a realtime voice resource profile;
2. give each component a residency/resource budget;
3. keep lightweight stages on CPU/ONNX where appropriate;
4. allow the supervisor to reject profiles that cannot coexist safely;
5. benchmark combinations, not only individual models;
6. expose residency/startup cost in realtime metrics.

The first milestone can prioritize one known-good English profile over model variety.

## 14. Streaming TTS

Keep sentence/phrase chunking, but improve it.

Requirements:

- start synthesis before the complete LLM answer exists;
- prefer clause/phrase boundaries over arbitrary character limits;
- keep audio segments small and independently cancellable;
- maintain a queue generation ID;
- never enqueue stale TTS after interruption;
- optionally synthesize the next phrase while the current phrase plays;
- preserve punctuation/prosody hints when supported.

Measure:

- first usable text -> TTS request;
- TTS request -> first audio;
- queued audio duration;
- cancellation waste.

## 15. Foreground tools vs durable background work

Realtime voice must not block the conversation on long jobs.

### Foreground

Suitable for work expected to complete quickly:

- Home Assistant/MQTT command;
- simple status query;
- lightweight MCP lookup;
- short database/tool operation.

The active response can wait for these.

### Background

Delegate work such as:

- repository changes;
- coding agents;
- compilation/test campaigns;
- deep research;
- long benchmarks;
- long external operations.

Return a durable `TaskHandle` immediately.

Required task fields:

- `task_id`
- parent conversation/session
- originating invocation
- accepted instruction
- status
- progress events
- permission envelope
- result
- created/completed timestamps
- delivery status

The user must be able to continue talking, ask for status, modify/cancel a task, disconnect, reconnect, and receive the result later.

Distinguish:

- `task.completed`
- `task.result_delivered`

A media disconnect must never cancel a background task unless explicitly requested.

## 16. Conversation context

Do not append an unlimited raw transcript to every prompt.

Maintain:

1. short live dialogue window;
2. running conversation summary;
3. active tasks/tool state;
4. retrieved relevant long-term context;
5. canonical AI-Stack Invocation/TraceContext;
6. optional provider-native acoustic context.

Summaries should be versioned and tied to the conversation, not the transport connection.

## 17. Reconnection

Current WebSocket sessions are removed on disconnect. Realtime v2 should support bounded reconnection.

Separate:

- media connection lifetime;
- logical conversation lifetime;
- background-task lifetime.

Reconnection requirements:

- short-lived reconnect ticket;
- last acknowledged event sequence;
- active task list;
- last committed turn;
- no replay of already-heard audio;
- stale response cancellation;
- explicit `session.reconnecting` / `session.updated` events.

Do not attempt to resume half-played assistant audio by default. Resume logical state, then generate a clean continuation if needed.

## 18. Pipecat integration

Use Pipecat for realtime media/pipeline orchestration, not as AI-Stack's canonical agent architecture.

Good uses:

- transport adapters;
- frame processing;
- realtime pipeline lifecycle;
- VAD integration;
- service/provider glue;
- interruption primitives.

Do not duplicate inside Pipecat:

- canonical Invocation;
- Tool Broker;
- permissions;
- long-running task ownership;
- AI-Stack model routing;
- supervisor resource policy.

The Pipecat layer should adapt to AI-Stack, not become a second control plane.

## 19. LiveKit and telephone calls

After WebRTC browser voice is reliable:

1. add a self-hosted LiveKit deployment;
2. implement LiveKit transport adapter;
3. move browser/desktop realtime sessions to it;
4. preserve the same voice runtime and protocol;
5. then add LiveKit SIP/telephony service.

Telephone-specific profile differences:

- narrowband/wideband codec handling;
- aggressive echo/noise tolerance;
- DTMF events;
- call connect/disconnect lifecycle;
- phone caller identity policy;
- stricter permissions for unauthenticated callers;
- recording policy controls.

Do not create a separate phone agent.

## 20. Native speech-to-speech providers

Native realtime audio models are a provider family, not the foundation.

Implement only after transport/session/cancellation interfaces are stable.

Provider acceptance requirements:

- streaming audio input/output;
- interruption/cancel semantics;
- text/tool event extraction when supported;
- provider timeout/reconnect behavior;
- traceability;
- deterministic fallback to cascaded mode when configured.

Hosted/native providers can be used as reference-quality benchmarks without making AI-Stack depend on them.

## 21. Observability

Extend current tracing rather than replacing it.

Per turn, record spans/events for:

- transport receive;
- VAD speech start;
- EOT decision;
- STT first partial/final;
- Invocation creation;
- first LLM token;
- each tool call;
- background task delegation;
- TTS request;
- first TTS chunk;
- first audio sent;
- first audio played/acknowledged when available;
- barge-in detection;
- playback stop;
- response completion/cancellation.

Important metrics:

- speech start -> VAD detection;
- true user end -> turn commit;
- turn commit -> first transcript;
- turn commit -> first LLM token;
- first usable text -> first TTS audio;
- user end -> first audible assistant audio;
- barge-in -> assistant silence;
- false end-of-turn rate;
- user interruption count;
- assistant interruption count;
- abandoned responses;
- packet loss/jitter;
- reconnect count;
- tool latency;
- task latency;
- GPU/CPU/VRAM by voice profile.

Avoid unbounded metric labels. Keep session/turn IDs in traces/logs, not Prometheus dimensions.

## 22. Evaluation harness

Integrate realtime voice evaluation into the existing eval-router instead of creating a disconnected benchmark framework.

Fixture categories:

- clean single-turn speech;
- hesitations and filler words;
- long pauses that are not turn endings;
- genuine turn endings;
- rapid self-correction;
- user barge-in;
- assistant cancellation during TTS;
- background noise;
- echo/playback leakage;
- packet delay/loss simulation;
- reconnect mid-session;
- foreground tool call;
- delegated long-running task;
- task completion after disconnect;
- stale generation/result injection.

Record:

- original input audio;
- separate assistant output audio;
- transcript/event timeline;
- trace IDs;
- provider/profile;
- expected turn boundaries;
- expected interruption points;
- tool/task ground truth.

Scores:

- STT WER/CER where reference exists;
- end-of-turn precision/recall;
- false cut-off rate;
- interruption latency;
- first-audio latency;
- response semantic quality;
- tool correctness;
- task lifecycle correctness;
- audio continuity/glitch count.

## 23. Dashboard

Add a realtime voice diagnostics page.

Per active session show:

- state;
- profile/provider;
- transport;
- input/output levels;
- active turn/response;
- active tool/task;
- user-end -> first-audio latency;
- barge-in latency;
- packet/jitter stats when available;
- STT/LLM/TTS timing waterfall;
- GPU residency/resource state.

Provide a development-only event timeline useful for debugging race conditions.

## 24. Security

Preserve current one-use session ticket concept.

Add:

- transport-scoped short-lived credentials;
- explicit per-session tool permissions;
- maximum session duration;
- maximum audio/frame sizes;
- rate limits;
- bounded reconnect window;
- phone caller permission policy;
- no secrets in events/traces;
- no direct Docker socket access;
- no bypass around gateway/supervisor authentication.

## 25. Latency acceptance targets

These are engineering goals, not assumptions about any specific model.

| Metric | Initial acceptable | Target |
| --- | ---: | ---: |
| VAD speech-start detection | <150 ms | <80 ms |
| confirmed barge-in -> silence | <250 ms | <120 ms |
| partial transcript | <500 ms | <250 ms |
| true turn end -> commit | <700 ms | 250-450 ms |
| commit -> first LLM token | <500 ms | <250 ms |
| usable text -> first TTS audio | <500 ms | <250 ms |
| user end -> audible assistant | <1.5 s | 500-900 ms |
| false turn termination | <5% | <1-2% |

P95 values must also be tracked; median-only success is insufficient.

## 26. Implementation phases

### Phase 0 - preserve and characterize current system

- [x] Keep current WebSocket voice tests green.
- [ ] Add event-sequence fixtures for current behavior.
- [ ] Add benchmark fixtures for latency/barge-in.
- [x] Document current v1 protocol.
- [x] Emit a bounded rolling p50/p95 summary for available first-token and first-audio timings.
- [x] Add generation IDs before deeper refactors.

**Done when:** current behavior is reproducible and race regressions can be detected.

### Phase 1 - internal architecture refactor

- [x] Introduce transport-independent `RealtimeSession`.
- [x] Extract protocol schemas.
- [x] Extract runtime/state machine from FastAPI transport.
- [x] Formalize cancellation/generation ownership.
- [ ] Convert `GatewayVoiceProviders` into the explicit `CascadedRealtimeProvider` adapter.
- [x] Preserve v1 WebSocket compatibility.

**Done when:** the same tests pass through the new runtime abstraction.

### Phase 2 - WebRTC + Pipecat

- [x] Add Pipecat integration.
- [x] Add local/self-hosted WebRTC transport.
- [x] Build the Dashboard browser client.
- [x] Add audio format conversion.
- [ ] Verify browser AEC/noise suppression.
- [x] Add transport metrics.

**Status:** transport and client implementation are present. Close acceptance only after real browser conversation and the separate remote TURN checks pass; the current automated UI tests use mocked transport behavior.

### Phase 3 - turn-taking quality

- [ ] Add Silero VAD or equivalent.
- [ ] Add semantic/hybrid end-of-turn detector.
- [ ] Add hesitation/non-terminal pause corpus.
- [ ] Tune profile thresholds.
- [ ] Keep manual commit path.

**Done when:** turn cut-offs meet acceptance targets on the fixture suite.

### Phase 4 - interruption/playback correctness

- [x] Clear queued WebRTC audio when cancelling a response.
- [ ] Track playback cursor/acknowledgement.
- [x] Reject stale generation events.
- [x] Add barge-in cancellation regression coverage.
- [ ] Measure p50/p95 stop latency.

**Done when:** stale speech cannot resume and barge-in latency meets the initial target.

### Phase 5 - tools and durable background tasks

- [ ] Route foreground tools through Tool Broker.
- [ ] Define `TaskHandle`.
- [ ] Add durable task store/lifecycle.
- [ ] Add task status/modify/cancel voice events.
- [ ] Reinject completed task results into conversation context.
- [ ] Add disconnect/reconnect task tests.

**Done when:** a long coding/eval job can outlive the voice connection and report completion later.

### Phase 6 - voice profiles and resource scheduling

- [ ] Add declarative voice profile schema.
- [ ] Add supervisor resource negotiation.
- [ ] Benchmark cascaded combinations on RTX 3060.
- [ ] Add `local-fast`, `local-quality`, and `offline`.
- [ ] Surface profile/resource status in dashboard.

**Done when:** resource feasibility is deterministic rather than incidental.

### Phase 7 - richer observability/evaluation

- [x] Add stage spans and per-turn first-token/first-audio timing.
- [ ] Add full client/server timing waterfall, including playback acknowledgement.
- [ ] Add audio/event recording fixtures.
- [ ] Add interruption/EOT scoring.
- [ ] Connect results to eval-router.
- [ ] Add dashboard realtime diagnostics.

**Done when:** voice regressions produce inspectable evidence rather than subjective reports.

### Phase 8 - native realtime speech providers

- [ ] Implement `NativeRealtimeProvider`.
- [ ] Add at least one reference provider.
- [ ] Add local native provider when a suitable model is supported.
- [ ] Compare native vs cascaded quality/latency/tool behavior.
- [ ] Preserve provider-independent client protocol.

**Done when:** changing provider does not require client/runtime rewrites.

### Phase 9 - LiveKit + calls

- [ ] Deploy self-hosted LiveKit.
- [ ] Add LiveKit transport.
- [ ] Migrate browser/desktop transport if evaluation is positive.
- [ ] Deploy SIP bridge.
- [ ] Add phone profile and caller permissions.
- [ ] Add call lifecycle/eval fixtures.

**Done when:** a phone call and a browser voice session exercise the same `RealtimeSession` and tool/task runtime.

## 27. Next implementation sequence

The original first-PR proposal is superseded: typed schemas, generation identity, runtime extraction, cancellation ownership, v1 compatibility, and the initial WebRTC path are already in this branch. Do not repeat that foundation work as a new milestone.

1. Finish and review the current speech API/UI, STT, and TTS startup changes as separate changesets.
2. Run the remote TURN acceptance procedure from a second Tailnet device and record the selected ICE candidate pair and call lifecycle results.
3. Build the turn-taking fixture set first, then compare VAD-only and hybrid end-of-turn decisions against it.
4. Add a playback acknowledgement event/cursor; use it to make interrupted history reflect audio actually heard, then measure p50/p95 barge-in-to-silence latency.
5. Define durable `TaskHandle` ownership and disconnect/reconnect semantics before adding more voice tool surface.
6. Expand eval-router fixtures and Dashboard diagnostics to cover the measured turn, playback, interruption, and task behavior.

Keep native speech-to-speech providers and LiveKit/SIP behind those acceptance gates. They should reuse the runtime, provider, and task contracts instead of introducing a parallel voice stack.

## 28. Definition of the final result

The target is not merely "speech input and TTS output."

AI-Stack should provide a persistent callable interface where a user can:

- start a browser/desktop/phone conversation;
- speak naturally with pauses and corrections;
- hear the assistant start responding quickly;
- interrupt it naturally;
- invoke Home Assistant/MQTT/MCP/internal tools;
- delegate a long coding/research/eval task without freezing the call;
- ask for task progress or modify/cancel it;
- disconnect and later reconnect;
- receive a completed task result after reconnection;
- switch between cascaded and native realtime speech providers without changing the client;
- inspect exact latency, turn-taking, tool, audio, and resource behavior in the dashboard/eval system.

All of that must continue to respect AI-Stack's gateway, supervisor, security, tracing, and resource-control invariants.
