# Architecture Notes

## Control plane

The gateway owns authentication, logical model routing and response proxying.
The supervisor owns Docker lifecycle and GPU exclusivity. It is reachable only on the internal `ai-stack-net` network.
The CPU-only voice service handles realtime session state, VAD, and audio-to-text-to-audio orchestration. It can reach the gateway over a separate private network; the gateway proxies authenticated voice sessions to clients. Voice calls every model through the gateway and has no Docker socket or direct backend access.

The gateway translates public chat, Responses, audio, vision, and realtime-session requests into canonical invocations. Provider spans carry low-cardinality invocation, model, provider, and service attributes; streaming provider spans stay open through stream cleanup and record first-token timing. The voice service creates a turn span and STT, LLM, and TTS child spans, forwarding each child trace context back through the gateway. Set `OTEL_EXPORTER_OTLP_ENDPOINT` on the gateway and voice service to export spans to an OTLP/HTTP collector.

Inbound MCP model tools extract W3C trace context from the MCP request and forward it to the gateway. Outbound MCP tools are discovered from host-supplied `MCP_SERVERS_JSON`, filtered by exact allowlists, and called only through the permission-checked Tool Broker. `MCP_SERVER_TOKENS_JSON` maps environment-variable names to host-supplied credentials.

This separation deliberately keeps `/var/run/docker.sock` out of the network-facing gateway container.

## Agent Lab trust boundary

Agent Lab is a separate CPU-only control-plane service for LangGraph experiments. It can call the authenticated gateway, but experimental workers never receive write access to the live checkout. The container sees the host repository's `.git` directory read-only and clones a private bare mirror under `data/agent-lab`; every run gets a detached worktree from that mirror.

The Agent Lab controller has a read-only root filesystem, drops all Linux capabilities, has no Docker socket, and does not mount `.env`, model directories, the host home directory, or the live worktree. Candidate commits are stored only in private refs under the Agent Lab mirror until a future promotion layer explicitly accepts them.

Evaluation is split again at the code-execution boundary. `agent-lab-sandbox` uses the same versioned image but runs with `network_mode: none`, receives no AI credentials, mounts run workspaces read-only, and communicates with the controller through a file queue. The daemon keeps the queue root-only and demotes each test process to an unprivileged UID. The sandbox retains only SETUID/SETGID capabilities needed for that demotion.

The v0.3 model interface is intentionally narrow: repository context is bounded and generated changes are limited to at most five preflighted replace/create operations. Paths must remain inside the run worktree; tests, hidden holdouts, secret/environment paths, and Git metadata are protected by enforcement rather than prompt text alone. Patch validation completes before any file is written, and a generic passing baseline is not accepted as proof of task completion.

Visible regression checks may drive repair iterations, but hidden holdout tests are excluded from model context and execute only after a visible pass. Active cancellation is cooperative between graph stages and blocks candidate promotion once requested. On controller restart, stale preparing/running/cancelling records are moved to an error state so they cannot remain permanently active.

Benchmark history is persisted separately from run state. The most recent execution and the known-good full-suite reference are separate records: subset/debug runs cannot move the reference. Compatible full runs are compared case-by-case, and the standard benchmark command fails on any current case failure or regression from the reference. The reference advances only after a full suite completes with no failures and no regressions. Candidate promotion review is read-only: it validates private-ref integrity, current-base freshness, validation evidence, diff scale, binary/deletion changes, and protected paths. Automatic promotion does not exist. Agent Lab/control-plane self-modifications are blocked by this gate until the candidate itself can be built and benchmarked in an isolated evaluation environment. The immutable controller/promotion boundary therefore remains outside self-modifying agent and harness code.

## GPU lifecycle

For a request targeting a GPU service:

1. Gateway serializes the heavyweight job.
2. Gateway asks supervisor to ensure the requested service.
3. Supervisor stops any other running GPU service.
4. Supervisor starts the target container.
5. Supervisor polls the service-specific readiness endpoint.
6. Gateway forwards the request.
7. The service remains warm until another GPU service is requested or `stop-all` is called.

This avoids repeated reloads during bursts while guaranteeing that two large runtimes do not compete for 12 GB VRAM.

Realtime voice retains conversation state across a session but follows the same exclusive GPU lifecycle for each STT, LLM, and TTS stage. The gateway forwards the `voice` workload profile as scheduling intent; the supervisor still enforces single-worker admission. It does not imply a pinned multi-model lease or guarantee that a model stays loaded between stages. Verify compatible GPU memory and scheduler support before enabling co-resident voice workers.

## Build isolation

Supervisor, STT and VLM build from their own narrow directories. Gateway, voice and MCP use repository-root contexts so their images can include shared `core` and `observability` packages; Dockerfile-specific ignore files allow only the required service and shared-package trees. Model files, runtime data, and environment files remain outside every build context.
ComfyUI builds from its repository with a dedicated `.dockerignore`.
WanGP only sends its Dockerfile, requirements and entrypoint into the build context.

Model files are runtime mounts, never Docker build inputs.

## API behavior

Chat model IDs are logical aliases:
- `local-fast`
- `local-reasoning`

The gateway supports buffered OpenAI-compatible chat responses and streaming pass-through.
`/v1/responses` supports stateless text input, instructions, function tool declarations, and buffered or streaming output. It rejects stateful conversation IDs, non-text inputs, and unsupported reasoning controls. If a stream ends without a completion marker, the gateway emits `response.failed` and does not report partial output as successful.
Other modalities retain dedicated endpoints:
- `/v1/audio/transcriptions`
- `/v1/audio/speech`
- `/v1/vision/analyze`
- `/v1/comfy/*`
- `/v1/wangp/*`

Realtime voice is exposed through `POST /v1/realtime/sessions` and the authenticated `/v1/realtime` WebSocket bridge. Session tickets are short-lived, one-use WebSocket subprotocols. The current audio contract is mono PCM16 at 16 kHz input and PCM16 output. Server VAD uses a 650 ms silence hangover by default. English is the only supported language. The Whisper adapter returns a completed transcription before segment deltas are emitted, so these are not streaming interim recognition results.

Voice turn health events are sent to the internal evaluation router and include timings, byte counts, interruption/truncation flags, and provider identifiers. Transcript and audio contents are omitted. Evaluation forwarding is fail-open and Opik reporting is optional.

Invocation and voice-turn screens are persisted with optional Opik reporting. Suspicious events can be claimed from the durable escalation queue; evaluator callbacks have a 30-second default deadline and claims recover after 120 seconds without an update. The resource scheduler defaults to exclusive `compatibility` mode. Its opt-in `resource` mode admits a worker only when measured worker memory, configured GPU capacity, reserved headroom, and workload compatibility all support the request; unset measurements fail closed.

## Failure behavior

Supervisor startup failures include the target container's recent logs.
Each service has an explicit cold-start timeout in `config\models.json`.
The control plane can report status even when no GPU backend is running.
