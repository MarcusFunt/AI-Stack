# AI-Stack local setup guide

This guide shows how to open the local stack, check its setup, and finish the optional GPU smoke run on the RTX 3060.

> **About the screenshots:** The panels below are simulated examples, not captured screenshots. Names and live resource values can differ on your machine.

## Current setup

The core local stack is already installed and running:

- Dashboard: `http://127.0.0.1:3000`
- Gateway: `http://127.0.0.1:8090`
- Docker Compose, NVIDIA GPU access, and the Windows host agent are available.
- EmbeddingGemma 2 is installed at `models/embedding/embeddinggemma-2` and runs on CPU.
- The visual-memory database and HNSW index are initialized.
- The visual-memory model has completed a real image-index and text-search request.
- `scripts\ai.ps1 doctor` passes.

The local AI API key is present in the running gateway, and authenticated requests worked. Repository instructions prohibit reading or changing `.env` files, so this guide leaves them untouched. No OpenAI API key or other cloud credential is required for visual memory.

The only unfinished item is the full GPU smoke suite. The local-fast and reasoning models passed their smoke checks. The run paused before validating TTS, STT, VLM, ComfyUI, and WanGP because Windows free RAM fell below 1 GiB while the computer was busy. Nothing in this guide requires stopping your other work; run the remaining GPU checks when the machine has enough headroom.

## 1. Open the stack

Open Windows PowerShell and change to the repository:

```powershell
Set-Location D:\AI-Stack
```

Start the local stack and open the dashboard:

```powershell
.\scripts\ai.ps1 launch
```

The dashboard should be available at [http://127.0.0.1:3000](http://127.0.0.1:3000).

### Simulated screenshot: dashboard

```text
┌──────────────────────── AI-Stack ─────────────────────────┐
│ Overview                                                   │
│                                                            │
│ Gateway          ● Healthy      127.0.0.1:8090            │
│ Dashboard        ● Healthy      127.0.0.1:3000            │
│ Visual memory    ● Ready         EmbeddingGemma 2 · CPU    │
│ GPU owner        ○ None                                      │
│ Active GPU jobs  0                                           │
│                                                            │
│ Models are loaded on demand by the supervisor.              │
└────────────────────────────────────────────────────────────┘
```

## 2. Check the local setup

Run the repository status and doctor commands:

```powershell
.\scripts\ai.ps1 status
.\scripts\ai.ps1 doctor
```

Look for `ALL_DOCTOR_CHECKS_PASSED`. The doctor checks Docker, GPU detection, required model files, service health, the visual-memory database/index, and container isolation. It reports whether the protected Compose environment file exists without displaying its entries.

### Simulated screenshot: successful doctor

```text
[PASS] docker: Docker version ...
[PASS] gpu: NVIDIA GeForce RTX 3060, 12288 MiB, ...
[PASS] visual-memory-model: google/embeddinggemma-2@...; device=cpu; loaded=True
[PASS] visual-memory-database: writable; records=1; observations=1; vectors=1
[PASS] visual-memory-index: hnswlib; ... bytes
ALL_DOCTOR_CHECKS_PASSED
```

The one record shown above is a small generated smoke fixture stored in the separate `smoke:visual-memory` namespace.

## 3. Check available GPU and system memory

The full suite loads heavyweight services one at a time through the supervisor. The reasoning model is large and took several minutes to load during the earlier run. Check current headroom before starting the full suite:

```powershell
Get-CimInstance Win32_OperatingSystem |
  Select-Object @{Name='Free RAM GiB';Expression={[math]::Round($_.FreePhysicalMemory / 1MB, 1)}}

nvidia-smi --query-gpu="name,memory.used,memory.free,memory.total,utilization.gpu" --format=csv
```

I recommend waiting until Windows has around 8 GiB or more free RAM before the full run. That is a practical cushion based on the earlier memory pressure, not a repository-enforced threshold. You can leave other applications alone and run this later when they are using less memory.

Before the suite begins, `scripts\smoke.py` checks that no heavyweight GPU service or active job is already running. If it reports a busy GPU, let that work finish and try again; do not stop a job to make the smoke test run.

## 4. Run the full GPU smoke suite

The smoke script needs `AI_API_KEY` in the current PowerShell process. If it is not already set there, this command copies it from the running gateway’s process environment without printing the value or reading `.env`:

```powershell
$hadApiKey = Test-Path Env:AI_API_KEY
$previousApiKey = $env:AI_API_KEY

try {
    if (-not $env:AI_API_KEY) {
        $env:AI_API_KEY = (& docker exec ai-stack-gateway python -c "import os,sys; sys.stdout.write(os.environ['AI_API_KEY'])").Trim()
    }
    if (-not $env:AI_API_KEY) {
        throw 'AI_API_KEY was not available in the running gateway.'
    }

    .\scripts\smoke.ps1
} finally {
    if ($hadApiKey) {
        $env:AI_API_KEY = $previousApiKey
    } else {
        Remove-Item Env:AI_API_KEY -ErrorAction SilentlyContinue
    }
    Remove-Variable previousApiKey -ErrorAction SilentlyContinue
}
```

The suite exercises authenticated gateway routing, local-fast chat, reasoning, TTS, STT, Qwen vision, ComfyUI, and WanGP. The supervisor switches heavyweight GPU services serially. Expect model startup to take minutes, especially for reasoning. A complete run ends with:

```text
ALL_SMOKE_TESTS_PASSED
GPU service release remains with supervisor leases and idle timers.
```

### Simulated screenshot: completed GPU smoke

```text
[PASS] gpu-preflight: idle; no active jobs or heavyweight services
[PASS] llm-buffered: OK
[PASS] llm-streaming: OK
[PASS] reasoning-inference: ...
[PASS] tts: ... bytes
[PASS] stt: ...
[PASS] vlm: ...
[PASS] comfyui: NVIDIA GeForce RTX 3060
[PASS] wangp: HTTP 200
ALL_SMOKE_TESTS_PASSED
```

## 5. Confirm the GPU is idle afterwards

Check the supervisor state when the suite finishes:

```powershell
.\scripts\ai.ps1 status
```

Look for `gpu_owner: null`, `running_gpu_services: []`, and `active_jobs: {}`. GPU services are demand-loaded; their stopped state after the supervisor’s idle timers is normal.

## If a check fails

- For missing services or stale images, run `.\scripts\ai.ps1 doctor` and use the specific failing check as the starting point.
- For memory pressure, wait for your current computer workload to finish, check RAM and GPU headroom again, and rerun the smoke suite.
- For an API authentication error, confirm the gateway container is healthy and retry the environment-variable block above. Do not paste or print the key.
- For detailed visual-memory instructions and API examples, see [visual-memory.md](visual-memory.md).
