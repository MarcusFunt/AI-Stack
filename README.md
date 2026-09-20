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
```

Only one heavyweight GPU service owns the GPU at a time. The gateway, supervisor, telemetry sampler, dashboard, and MCP bridge stay resident.

Open **http://127.0.0.1:3000** for the Local AI control surface. It provides the instrument-style Overview, Models, Jobs, Health and Setup pages in addition to fast/reasoning chat, STT, TTS, robotics vision, image/video studio launchers, model registry information, and service controls. Browser requests go through the dashboard's `/api` proxy; `AI_API_KEY` is injected server-side and is not stored in frontend code or browser storage.

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

The MCP exposes only status, model discovery, API capability discovery, and `ask_local_ai`. It does not expose shell, arbitrary files, Docker, or remote-desktop functions.

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
Maintenance:

```powershell
.\scripts\ai.ps1 doctor
.\scripts\ai.ps1 model-info
.\scripts\ai.ps1 smoke
.\scripts\ai.ps1 test-leases
.\scripts\ai.ps1 test-proxy
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

## Logical models

- `local-fast` -> Qwen3.5-9B / llama.cpp
- `local-reasoning` -> Qwen3.8-27B / llama.cpp hybrid CPU+GPU
- `local-stt` -> faster-whisper large-v3
- `local-tts` -> Qwen3-TTS
- `local-vlm` -> Qwen3-VL
- `local-image` -> ComfyUI
- `local-video` -> WanGP

The registry lives at `config\models.json` and is exposed at `/v1/models`.
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
