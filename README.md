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
Gateway :8090   -- authenticated public/local API
  |
  v
Supervisor     -- internal Docker/GPU lifecycle service
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

Only one heavyweight GPU service owns the GPU at a time. The gateway, supervisor, and dashboard stay resident.

Open **http://127.0.0.1:3000** for the Local AI control surface. It provides live service/GPU state, fast and reasoning chat, STT, TTS, robotics vision, image/video studio launchers, model registry information, and service controls. Browser requests go through the dashboard's `/api` proxy; `AI_API_KEY` is injected server-side and is not stored in frontend code or browser storage.
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
| `local-fast` | Qwen3.5-9B | 5.290 GiB | 262,144 | 16,384 | ~7.4 GiB | ~53 tok/s |
| `local-reasoning` | Qwen3.8-27B | 17.671 GiB | 262,144 | 8,192 | ~10.25 GiB | ~4.8 tok/s |

The smaller runtime contexts are deliberate so KV cache does not crowd model weights on a 12 GB GPU.
Both GGUFs advertise GGUF v3, quantization version 2, file type 15. Metadata can be re-read without loading a model:

```powershell
.\scripts\ai.ps1 model-info
```

Latest measured fast-model run:
- prompt: ~380 tok/s on a 25-token prompt
- generation: ~53.1 tok/s
- VRAM: ~7.4 GiB
- `LLM_GPU_LAYERS=999` (full offload)

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
.\scripts\ai.ps1 bench llm
.\scripts\ai.ps1 bench reasoning
.\scripts\ai.ps1 logs reasoning
.\scripts\ai.ps1 build
.\scripts\ai.ps1 update
.\scripts\ai.ps1 rollback
```

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

Keep the lightweight gateway and supervisor running. Heavy GPU containers remain stopped until requested.
After manual testing:

```powershell
.\scripts\ai.ps1 stop-all
```

Use `doctor` before or after significant changes. It checks Docker, Compose, the RTX 3060, required secrets, model files, Compose validity, GPU exclusivity, and disk headroom.
