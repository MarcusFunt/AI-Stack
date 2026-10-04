# Local AI Stack

Single-GPU multimodal AI server for the RTX 3060 12 GB / 32 GB Windows desktop.

## Architecture

```
Browser
  |
  v
Dashboard :3000 -- React/Vite control surface + server-side API-key proxy
  |
  v
Gateway :8090   -- authenticated public/local API + jobs/MQTT adapter
  | \
  |  +--> Telemetry -- NVML + CPU/RAM/storage sampler (1 Hz)
  v
Supervisor     -- internal Docker/GPU lifecycle + semantic state service
  |
  +-- llama.cpp fast LLM
  +-- llama.cpp reasoning LLM
  +-- faster-whisper
  +-- Qwen3-TTS
  +-- Qwen3-VL
  +-- ComfyUI
  +-- WanGP
  +-- LeRobot (optional)

Gateway <--> Voice -- internal realtime audio session service (CPU-only)
```

Only one heavyweight GPU service owns the GPU at a time. The gateway, supervisor, telemetry sampler, dashboard, and MCP bridge stay resident.

Open **http://127.0.0.1:3000** for the Local AI control surface. It provides the instrument-style Overview, Models, Jobs, Health and Setup pages in addition to fast/reasoning chat, STT, TTS, robotics vision, image/video studio launchers, model registry information, and service controls. Browser requests go through the dashboard's `/api` proxy; `AI_API_KEY` is injected server-side and is not stored in frontend code or browser storage.

### Start and update

For a cold start on Windows, double-click `scripts\start-ai-stack.cmd`, or run:

```powershell
.\scripts\ai.ps1 launch
```

The launcher starts Docker Desktop when needed, checks the existing Windows Tailscale client without changing Serve/Funnel routes, starts the resident control plane, waits for Gateway and Dashboard health, then opens the Dashboard. If Tailscale is unavailable, local startup continues. Normal startup does not pull or build code. Use **Dashboard → Operate → Maintenance → Refresh dependencies** or `.\scripts\ai.ps1 update` to explicitly update; updates require a clean worktree and idle GPU and fast-forward the current branch to `origin/main` when safe.

## Remote access

Tailscale keeps normal Local AI access private to the tailnet:
- UI: `https://marcus-computer.taile97c31.ts.net:8443/`
- API through the dashboard proxy: `https://marcus-computer.taile97c31.ts.net:8443/api/v1/...`
- Existing Ascento dashboard remains on private Tailscale HTTPS port 443.

The remote MCP bridge is the only public Funnel service:
- Bearer-auth endpoint: `https://marcus-computer.taile97c31.ts.net:10000/mcp`
- Connector-compatible secret URL: run `.\scripts\show-mcp-connection.ps1` locally to display it.

Operational helpers:
```powershell
.\scripts\tailscale-local-ai.ps1
.\scripts\tailscale-local-ai.ps1 -StatusOnly
.\scripts\tailscale-local-ai.ps1 -DisablePublicMcp
.\scripts\show-mcp-connection.ps1
.\scripts\rotate-mcp-credentials.ps1
```

The MCP exposes status, model discovery, API capability discovery, `ask_local_ai`, audio transcription, and English-only speech synthesis. Audio tools use base64 for file/audio payloads; synthesis supports all nine Qwen3-TTS CustomVoice speakers, expressive `instruct` guidance, speed, and output format. It does not expose shell, arbitrary files, Docker, or remote-desktop functions.

See [Audio API](docs/audio-api.md) for REST and MCP request fields, English-only TTS behavior, supported transcript formats, and examples.

## Realtime voice

Create an authenticated session with `POST /v1/realtime/sessions` using the gateway API key and an optional `{"language":"en"}` body. The response includes a short-lived, one-use `client_secret` and a gateway `ws_url`. Connect with both WebSocket subprotocols: `ai-stack.voice.v1` and `ai-stack.ticket.<client_secret.value>`. The ticket is sent in the WebSocket handshake rather than a URL or later audio message.

The session accepts mono, little-endian PCM16 at 16 kHz, as binary WebSocket frames or base64 `input_audio_buffer.append` events. Server VAD ends a turn after 650 ms of silence; clients may also send `input_audio_buffer.commit`. Events include `conversation.item.input_audio_transcription.*`, `response.output_text.delta`, `response.audio.delta`, `response.cancelled`, and `conversation.item.truncated`. Each audio delta carries base64 PCM16 plus its sample rate and channel count. Send `response.cancel` to cancel a reply, or `session.close` to end a session.

This initial voice API supports English. Whisper produces a final transcription before the service emits its segment-based transcription deltas; they are not live interim ASR. Speech recognition, chat, and speech synthesis each go through the gateway, which keeps the supervisor as the only Docker/GPU lifecycle owner. The existing GPU scheduler runs one heavyweight worker at a time, so voice requests can switch between STT, LLM, and TTS workers and may reload a model between stages. The `voice` workload profile expresses scheduling intent; it does not pin multiple models in memory. Multi-worker residency is deferred until GPU capacity is measured and verified.

### Dashboard WebRTC test page

The Dashboard includes **Work → Realtime voice** for testing microphone capture and two-way WebRTC audio. Start the local control plane with `.\scripts\ai.ps1 start voice`, then open `http://127.0.0.1:3000`. The page creates a fresh session for each start or retry, requests echo cancellation, noise suppression, and automatic gain control, and displays the browser-reported settings. Stop ends the peer connection and releases the microphone. Signaling uses same-origin `/api/v1/realtime/...` routes; the API key stays in Dashboard Nginx and the short-lived session ticket is sent in a header. Audio travels over WebRTC media, with no WebSocket fallback.

The existing Dashboard Tailscale HTTPS address above also works from devices enrolled in the tailnet, including a device on the same physical LAN. A LAN connection without Tailscale is not supported; Dashboard and Gateway host ports remain loopback-bound.

The optional voice TURN relay is off by default. It requires `VOICE_TURN_SHARED_SECRET` and `VOICE_TURN_HOSTNAME` to be injected into the voice runtime, then an explicit **Voice TURN relay** opt-in in the Dashboard Network panel. The host agent exposes the relay only through private Tailscale Serve TLS TCP on port 8447; do not enable Funnel or publish a host UDP port. Port 8446 already serves another local endpoint. Treat remote relay as unverified until a second tailnet device completes two-way speech and WebRTC stats show a selected relay candidate pair.

For an on-demand non-realtime pipeline check, use **Operate → Maintenance → Run voice smoke** or `.\scripts\ai.ps1 voice-smoke` after starting the stack. It generates a short sentence with `local-fast`, synthesizes it with `local-tts`, then transcribes that same temporary MP3 with `local-stt` and verifies a known phrase. It refuses to start during active GPU work, reports stage timings, and deletes the audio after the run without persisting the transcript.

Compose voice settings (all optional) are:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `VOICE_MAX_SESSIONS` | `8` | Maximum active voice sessions. |
| `VOICE_SESSION_TOKEN_TTL_SECONDS` | `60` | Lifetime of the one-use WebSocket ticket. |
| `VOICE_SESSION_MAX_SECONDS` | `3600` | Maximum duration of one voice session over WebSocket or WebRTC. |
| `VOICE_VAD_END_SILENCE_SECONDS` | `0.65` | Silence hangover before a turn is transcribed. |
| `VOICE_VAD_SPEECH_THRESHOLD` | `420` | PCM16 RMS amplitude threshold for speech detection. |
| `VOICE_MAX_TURN_SECONDS` | `90` | Maximum buffered audio duration per turn. |

Voice turn evaluations contain timing, byte counts, interruption/truncation state, and provider identifiers. They do not include transcripts or audio. The evaluation router persists the deterministic screen and may forward it to Opik when Opik is enabled; reporting is fail-open for the voice session.

For an external offline API check, see [external_eval/README.md](external_eval/README.md). It uses Inspect AI's standard OpenAI-compatible model adapter and built-in scorer against the gateway.

## Responses API

`POST /v1/responses` supports stateless text input, instructions, function tool declarations, buffered results, and server-sent-event streaming. Conversation state such as `previous_response_id`, image/audio input, and reasoning controls are not implemented; include the full message history in each request. A stream is marked failed if the backend closes or errors before a completion marker, so clients must handle `response.failed` instead of treating partial output as complete.

## Outbound MCP tools

The MCP bridge can connect to up to 16 explicitly configured external MCP servers. `MCP_SERVERS_JSON` contains each server's ID, URL, exact `allowed_tools` list, and optional `auth_env` name. `MCP_SERVER_TOKENS_JSON` maps those environment-variable names to credentials supplied by the host. Keep credentials in environment values; do not put secret values in server configuration. Discovered tools are registered with the Tool Broker and remain unavailable unless their exact names and permissions are admitted.

## Evaluation router

The evaluation router stores metadata-only invocation and voice-turn screens. Suspicious events enter a durable escalation queue; an authenticated worker claims one item and posts a bounded result to `/v1/escalations/{id}/complete`. `DeepEvalWorker` defaults to a 30-second evaluator deadline, and abandoned claims return to the queue after 120 seconds. The Inspect AI example in `external_eval/` is an offline API check, separate from this queue.

## GPU resource scheduler

`SUPERVISOR_SCHEDULER_MODE` defaults to `compatibility`, which retains exclusive GPU admission. `resource` mode permits co-residency only when workload groups match and every worker's VRAM requirement plus configured headroom fits `resource_capacity` in `config/models.json`. Unknown capacity or worker requirements fail closed. The checked-in model registry leaves these measurements unset, so keep the default compatibility mode until measured values are recorded.

## Security

- Gateway binds to `127.0.0.1:8090` and requires `AI_API_KEY`.
- Supervisor has no host port and requires a separate `SUPERVISOR_TOKEN`.
- llama.cpp chat endpoints require `LLAMA_API_KEY`; the gateway injects it internally.
- Unauthenticated direct llama chat requests return 401.
- LLM/STT/TTS/VLM backend ports are internal-only.
- ComfyUI (`127.0.0.1:8188`) and WanGP (`127.0.0.1:7860`) are locally exposed for their native UIs.
- The gateway no longer mounts the Docker socket; only the internal supervisor does.
- Model files and caches live on D: and are not baked into images.

## Current LLMs

| Logical model | Actual model | GGUF size | Native context | Runtime context | VRAM | Generation |
|---|---|---:|---:|---:|---:|---:|
| `local-fast` | Qwen3.5-9B | 5.290 GiB | 262,144 | 32,768 | ~8.6 GiB total GPU use while loaded | ~53 tok/s* |
| `local-reasoning` | Qwen3.8-27B | 17.671 GiB | 262,144 | 8,192 | ~10.25 GiB | ~4.8 tok/s |

The smaller runtime contexts are deliberate so KV cache does not crowd model weights on a 12 GB GPU.
Both GGUFs advertise GGUF v3, quantization version 2, file type 15. Metadata can be re-read without loading a model:

```powershell
.\scripts\ai.ps1 model-info
```

Latest measured fast-model benchmark (taken before the OpenCode context increase):
- prompt: ~380 tok/s on a 25-token prompt
- generation: ~53.1 tok/s
- previous 16k-context VRAM: ~7.4 GiB
- current 32k-context whole-GPU usage while loaded: ~8.6 GiB
- `LLM_GPU_LAYERS=999` (full offload)

*The generation figure is the previous benchmark and should be re-benchmarked if a precise 32k-context performance figure is required.

Latest reasoning-model run:
- prompt: ~8.7 tok/s
- generation: ~4.8 tok/s
- VRAM: ~10.25 GiB
- `REASONING_GPU_LAYERS=28`

Raw benchmark JSON is stored in `data\benchmarks`.

## Automatic GPU release

Gateway requests use supervisor leases. When the last request for a GPU service ends, an idle-stop timer is scheduled:
- LLM / reasoning / VLM: 300 s
- STT / TTS: 180 s
- ComfyUI / WanGP via gateway: 600 s
A request for a different GPU service:
1. waits for active leases to finish,
2. cancels stale idle timers,
3. stops the previous GPU owner,
4. starts and health-checks the requested service,
5. acquires a lease only when the backend is ready.

This behavior has regression tests for:
- multiple concurrent requests to the same LLM,
- cross-service waiting (LLM -> TTS),
- streaming client disconnect cleanup,
- stale idle-timer cancellation.

## Control

```powershell
cd D:\AI-Stack
.\scripts\ai.ps1 status
.\scripts\ai.ps1 start llm
.\scripts\ai.ps1 start reasoning
.\scripts\ai.ps1 start stt
.\scripts\ai.ps1 start tts
.\scripts\ai.ps1 start vlm
.\scripts\ai.ps1 start comfyui
.\scripts\ai.ps1 start wangp
.\scripts\ai.ps1 stop-all
```

To rebuild and recreate a single GPU service after changing its image, use `.\scripts\ai.ps1 create tts`. The targeted create checks that no AI jobs are active and only replaces the named service.

Maintenance:

```powershell
.\scripts\ai.ps1 doctor
.\scripts\ai.ps1 model-info
.\scripts\ai.ps1 smoke
.\scripts\ai.ps1 voice-smoke
.\scripts\ai.ps1 test-leases
.\scripts\ai.ps1 test-proxy
.\scripts\ai.ps1 test-agent-lab
.\scripts\ai.ps1 bench-agent-lab
.\scripts\ai.ps1 burn-in
.\scripts\ai.ps1 bench llm
.\scripts\ai.ps1 bench reasoning
.\scripts\ai.ps1 logs reasoning
.\scripts\ai.ps1 build
.\scripts\ai.ps1 update
.\scripts\ai.ps1 rollback
```

## OpenCode

OpenCode v2 is integrated as a host-side coding agent. It runs on Windows so it can work directly on this checkout and use the existing PowerShell/Docker workflow, while all model traffic still goes through the authenticated AI-Stack gateway.

Integration files:
- `opencode.jsonc` defines the `ai-stack/local-fast` provider, Local AI MCP bridge, local-context compaction settings, and agent permissions.
- `AGENTS.md` gives OpenCode the repository architecture, safety invariants, and verification workflow.
- `scripts\opencode.ps1` manages the loopback OpenCode server and provides TUI, scripted-run, MCP/status, API, and diagnostic commands.
- `scripts\opencode-smoke.ps1` performs a real end-to-end coding-agent completion through the gateway/supervisor/LLM path.

Common commands:

```powershell
.\scripts\opencode.ps1 doctor
.\scripts\opencode.ps1 tui
.\scripts\opencode.ps1 run "Review the gateway and identify one concrete reliability issue."
.\scripts\opencode.ps1 mini
.\scripts\opencode.ps1 models
.\scripts\opencode.ps1 mcp
.\scripts\opencode.ps1 serve
.\scripts\opencode.ps1 pair
.\scripts\opencode-smoke.ps1
```

The OpenCode server binds only to `127.0.0.1:4096` and uses a generated credential stored only in the ignored `.env`. The coding agent is explicitly denied reads of `.env`/environment override files, asks before destructive Git/Compose operations, and denies Docker system pruning.

The primary coding model is `ai-stack/local-fast` with a 32,768-token runtime context. For deeper reasoning, OpenCode should call the attached `local-ai` MCP server's `ask_local_ai` tool in reasoning mode. The 8,192-token reasoning worker is intentionally not exposed as an OpenCode primary model because OpenCode's own instruction/tool context is too large for that runtime setting.

## Agent Lab

Agent Lab is the experimental LangGraph subsystem for reproducible local-agent development. Its API binds only to `127.0.0.1:8770`. A run is persisted in SQLite, resolved against a committed Git revision, and executed in a detached worktree created from Agent Lab's own bare mirror.

The controller does not mount the live working tree, models, or the Docker socket. It receives only the repository's `.git` directory read-only plus `data/agent-lab` as private writable state. The v0.3 worker gives the model a bounded repository context and accepts at most five preflighted file operations per iteration. It can replace exact text or create a small new source/config/documentation file, but it does not expose an unrestricted shell. Test files, hidden holdouts, `.git`, and environment files are protected by the patch layer unless test editing is explicitly enabled for a task.

Repository tests execute in a separate `agent-lab-sandbox` container. That sandbox has no network namespace, no AI/API credentials, a read-only mount of run workspaces, and only a root-owned file queue for returning structured results. The test subprocess is demoted to an unprivileged UID before repository code executes. This keeps arbitrary test/import code away from the controller's gateway credential and experiment database.

Current v0.3 workflow:
- `POST /runs` creates a persisted isolated run and worktree.
- `POST /runs/{id}/execute` starts the LangGraph repair loop.
- `GET /runs/{id}` and `GET /runs/{id}/events` expose structured status and history.
- `POST /runs/{id}/cancel` cancels queued runs immediately and active runs cooperatively. Active runs enter `cancelling` and cannot promote a candidate after cancellation is requested.
- `POST /runs/{id}/cleanup` removes a terminal run's private worktree while retaining its history.
- `GET /harnesses` lists available evaluation harnesses.
- `GET /benchmarks/latest`, `GET /benchmarks/reference`, and `GET /benchmarks/history` expose persisted benchmark evidence. `latest` is the most recent execution; `reference` is the full-suite known-good regression baseline.
- `GET /runs/{id}/promotion-review` performs a read-only candidate review. It checks run evidence, private candidate-ref integrity, base freshness, diff size, binary/deletion changes, protected paths, and self-modification policy. It never merges or mutates the source checkout.

The harness registry currently includes `python-unit` and `python-syntax`. Python-unit tasks can include a hidden `.agent_lab_holdout` suite: visible tests drive repair iterations, while hidden tests are excluded from model context and run only after a visible pass. By default a run must begin with a failing baseline so an unrelated already-green test suite cannot be treated as evidence that an objective was solved. Controller restarts recover stale preparing/running/cancelling records as errors instead of leaving them permanently in-flight.

The local benchmark is `agent-lab-core-v2`: 16 repair tasks covering behavioral bugs, hidden edge cases, syntax repair, new-module creation, combined create+repair changes, input-mutation safety, numeric parsing/clamping, and configuration merging. Benchmark `latest` and `reference` state is kept separately for each logical model, so `local-fast` and `local-reasoning` cannot overwrite each other's regression baselines. A subset/debug run can compare its selected cases against the same cases in a compatible full-suite reference, but only a completely green full-suite run may advance that model's reference. The previously stored 13-case `local-fast` reference remains usable only where its case set covers the requested comparison. `.\scripts\ai.ps1 bench-agent-lab` fails on any current case failure or compatible reference regression.

Promotion remains deliberately manual. A small non-Agent-Lab candidate can become eligible for manual promotion review, but automatic promotion is disabled. Changes to Agent Lab or its control-plane integration are explicitly blocked by the review gate until candidate-specific benchmark execution exists, so the subsystem cannot use its own current benchmark result as evidence for untested self-modification. The trusted boundary also includes `agent_eval/`, the self-improvement campaign runner, and its feature catalog, so candidates cannot rewrite their judge or promotion rules.

Self-improvement campaigns use `scripts/self_improve.py` (or `scripts/self-improve.ps1`). Each feature runs from a fixed base commit on its own branch. Candidate context can be scoped with `context_include` / `context_exclude`, keeping evaluator code and hidden validation definitions out of the prompt. A feature qualifies only when public and hidden validators both fail on the untouched baseline, both pass on the candidate inside the network-isolated read-only sandbox, the full repository unit suite remains green, repeated sealed evaluations preserve a stable reference, and normal promotion review stays eligible. Hidden-validator output is recorded as evidence but is not fed back into retries. Campaign evidence is stored under `data/state/self-improve-v2-*`; campaigns never auto-merge to `main`.

Run the isolated regression suite with `.\scripts\ai.ps1 test-agent-lab` and the real-model regression gate with `.\scripts\ai.ps1 bench-agent-lab`. Two higher-level entry points cover the broader testing pass:

```powershell
.\scripts\test-code.ps1
.\scripts\test-ai.ps1 -Model local-fast
.\scripts\test-ai.ps1 -Model local-reasoning
.\scripts\test-ai.ps1 -Model both -FullAgentCoding
```

`test-code.ps1` validates Compose/Python, runs Agent Lab and sealed-evaluator unit tests, and executes the sandbox privilege/secrecy check in the actual networkless sandbox container. `test-ai.ps1` first runs deterministic raw-model contract checks and then coding-agent tasks with visible tests plus hidden holdouts; it reports all stages before failing so one bad model check does not hide later coding results. Raw-model summaries are persisted under `data\agent-lab\model-quality`, while coding benchmark evidence remains under `data\agent-lab\benchmarks`. Multi-harness execution within a single run, model-token accounting, harness mutation, and automatic promotion remain deferred.

## Logical models

- `local-fast` -> Qwen3.5-9B / llama.cpp
- `local-reasoning` -> Qwen3.8-27B / llama.cpp hybrid CPU+GPU
- `local-stt` -> faster-whisper large-v3
- `local-tts` -> Qwen3-TTS CustomVoice, with named speaker selection in the dashboard
- `local-vlm` -> Qwen3-VL
- `local-image` -> ComfyUI
- `local-video` -> WanGP

The registry lives at `config\models.json` and is exposed at `/v1/models`.

The dashboard's Audio Lab lets you choose all nine Qwen3-TTS CustomVoice speaker timbres. Every synthesis request is set to English. Aiden and Ryan are native English voices. The gateway accepts the same speaker name in the OpenAI-compatible `voice` field. Set `TTS_MODEL` to `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` when overriding the model; the named presets require the CustomVoice model variant. See the [official Qwen3-TTS repository](https://github.com/QwenLM/Qwen3-TTS) for voice descriptions and language details.
## Updating and rollback

`ai.ps1 update` now:
1. refuses to overwrite tracked local changes in the vendored repositories,
2. records current ComfyUI/WanGP source SHAs,
3. tags currently working local images as rollback points,
4. preserves the current llama.cpp image as a rollback tag,
5. pulls upstream sources/images,
6. rebuilds,
7. **recreates stopped GPU containers** so future starts really use the new images,
8. restarts only the lightweight control plane.

A failed update can be reversed with:

```powershell
.\scripts\ai.ps1 rollback
```

To choose a specific snapshot:

```powershell
.\scripts\ai.ps1 rollback -Snapshot D:\AI-Stack\data\state\update-YYYYMMDD-HHMMSS.json
```
## Storage

- LLM GGUF: `models\llm`
- STT cache: `models\stt`
- Image models: `models\image`
- Video/WanGP models: `models\wangp`
- Shared caches: `data\cache`
- Outputs: `data\outputs`
- Benchmarks: `data\benchmarks`
- Update/rollback state: `data\state`

Vendored ComfyUI and WanGP repos use local `core.autocrlf=false` settings to avoid shell-script CRLF breakage on Windows.

## Operating policy

Keep the lightweight dashboard, gateway, supervisor, telemetry sampler, and MCP bridge running. Heavy GPU containers remain stopped until requested.
After manual testing:

```powershell
.\scripts\ai.ps1 stop-all
```

Use `doctor` before or after significant changes. It checks Docker, Compose, the RTX 3060, required secrets, model files, Compose validity, the Windows host agent/autostart entry, GPU exclusivity, Docker-socket isolation, running-image synchronization, local source fingerprints, dashboard proxy identity, and disk headroom. Use `burn-in` for the long regression pass: strict doctor before/after, forced gateway-IP replacement, lease concurrency and disconnect handling, plus end-to-end LLM/reasoning/TTS/STT/VLM/ComfyUI/WanGP checks. The transcript is written to `data\state\full-burnin-latest.log`.

## Dashboard telemetry and setup

The dashboard is also the machine's control/diagnostic panel. Its Overview separates the
**AI scheduler state** from physical GPU state, so an idle scheduler does not imply that
all 12 GiB of VRAM is free. A lightweight always-on telemetry container samples the RTX
3060 and runtime resources once per second.

The dashboard currently exposes:
- a global status strip and fleet matrix with semantic worker states: stopped, starting/loading, ready, running, unloading, and error;
- physical GPU load, VRAM, temperature, power and clock telemetry, deliberately separate from scheduler ownership;
- fixed-scale 2-minute / 10-minute / 1-hour / 24-hour / 7-day GPU, VRAM, temperature and power history charts, with hover readouts and minute history persisted under `data/state`;
- runtime CPU/RAM and model-drive headroom;
- current/recent jobs with phase, elapsed time, activity age, load ETA and LLM throughput;
- measured model cards with tokens/s, ms/token, prompt throughput, VRAM and cold-start time;
- a Health topology, GUI System Doctor, and one-click per-service or seven-service functional self-tests with live run progress and test-depth labels;
- a first-run Setup path that links prerequisites, API health, remote access, models and a full smoke test;
- a Network page that turns actual Tailscale Serve/Funnel state into structured route cards, warns about public exposure, and offers a private secure-defaults preset;
- a Models page with guarded typed runtime controls, installed-GGUF selection, Hugging Face downloads and progress when the CLI reports it;
- GUI configuration and broker testing for optional read-only MQTT/Home Assistant telemetry.

The browser receives a live snapshot over `/api/events` WebSocket with polling as a
fallback. The telemetry sidecar also exposes Prometheus-format metrics on its internal
container endpoint for an optional future Prometheus scraper; that endpoint is not
published directly to the host.

### MQTT / Home Assistant

MQTT is an output adapter only and is disabled by default. Local AI does not depend on
the MQTT broker being available, and the gateway does not subscribe to command topics.
Configure it from **Dashboard -> Setup** instead of editing configuration files.

The retained topic namespace starts at:

```text
localai/marcus-computer/status
localai/marcus-computer/gpu/utilization
localai/marcus-computer/gpu/vram_used_mb
localai/marcus-computer/gpu/temperature_c
localai/marcus-computer/gpu/power_w
localai/marcus-computer/scheduler/owner
localai/marcus-computer/job/state
localai/marcus-computer/service/<service>/state
```

When Home Assistant discovery is enabled, the gateway publishes discovery entries under
the configured discovery prefix (`homeassistant` by default). These same MQTT topics are
suitable for ESP32/OLED/LED status displays.


### Functional self-tests

**Dashboard -> Health** can run one service at a time or the complete GPU stack. The tests
use the normal supervisor lease path, so they also validate GPU hand-off rather than
starting containers behind the scheduler's back.

The full test exercises:
- real chat completions for the fast and reasoning LLMs;
- WAV decode/transcription for STT;
- actual WAV generation for TTS;
- an actual image-analysis request for the VLM;
- the ComfyUI API and WanGP application endpoint.

Heavy workers are still subject to their normal idle timers. Use **Stop all GPU services**
or `.\scripts\ai.ps1 stop-all` when a test session is finished if you want VRAM released
immediately.

### Windows host integration

Tailscale configuration and model-file management need access to Windows rather than the
Linux containers. `scripts\host_agent.py` provides a small token-authenticated bridge on
port 8788 for those allow-listed operations. It is not a shell API.

The bridge is started at Windows login through:

`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\LocalAIHostAgent.cmd`

The repository copy of the launcher is `scripts\start-host-agent.cmd`. The strict doctor
checks both the agent and its autostart entry. The agent reports Tailscale state and
Hugging Face CLI availability to the dashboard.

### Network / Tailscale GUI

**Dashboard -> Network** shows the actual Tailscale DNS name, Tailscale IP, private
dashboard route, MCP exposure mode and Windows bridge status. Route changes are applied
through the Windows host agent and the returned Tailscale state is re-read after each
change.

The intended layout remains:
- dashboard: tailnet-only HTTPS on port 8443;
- MCP: configurable as public Funnel, tailnet-only, or off on port 10000;
- direct local API: `127.0.0.1:8090`.

The page also detects the older HTTPS port-443 route and can remove it explicitly instead
of silently changing existing remote access.

### Model management

**Dashboard -> Models** exposes the allow-listed runtime settings from `.env` and can
download model files using the installed Hugging Face `hf` CLI. Worker recreation is
blocked while that worker is loaded or has active jobs; unload it first. Downloads are
written into the existing model directories on D: and do not automatically switch the
active model.
