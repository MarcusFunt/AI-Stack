#Requires -Version 5.1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Pass($name,$detail) { Write-Output ("[PASS] " + $name + ": " + $detail) }
function Fail($name,$detail) { Write-Output ("[FAIL] " + $name + ": " + $detail); $script:Failures++ }
function Warn($name,$detail) { Write-Output ("[WARN] " + $name + ": " + $detail) }
$script:Failures = 0

try { $dv = docker --version; Pass "docker" $dv } catch { Fail "docker" $_ }
try { $cv = docker compose version; Pass "compose" $cv } catch { Fail "compose" $_ }
try {
  $gpu = nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
  Pass "gpu" $gpu
} catch { Fail "gpu" $_ }

if(Test-Path -LiteralPath ".env" -PathType Leaf) {
  Pass "credential-config" "protected Compose environment file exists; entries were not inspected"
} elseif(@("AI_API_KEY","SUPERVISOR_TOKEN","LLAMA_API_KEY") | Where-Object { -not [Environment]::GetEnvironmentVariable($_) }) {
  Fail "credential-config" "required credentials are not present in the process environment"
} else {
  Pass "credential-config" "required credential names are present in the process environment"
}

foreach($file in @("models\llm\daily.gguf","models\llm\reasoning.gguf","config\models.json")) {
  if(Test-Path $file) {
    $item=Get-Item $file
    Pass "file:$file" (([math]::Round($item.Length/1GB,3)).ToString()+" GiB")
  } else { Fail "file:$file" "missing" }
}

$composeVars = Join-Path ([IO.Path]::GetTempPath()) ("ai-stack-doctor-" + [guid]::NewGuid().ToString("N") + ".vars")
try {
  [IO.File]::WriteAllLines($composeVars, @(
    "AI_API_KEY=doctor-placeholder",
    "SUPERVISOR_TOKEN=doctor-placeholder",
    "LLAMA_API_KEY=doctor-placeholder",
    "HOST_AGENT_TOKEN=doctor-placeholder",
    "MCP_API_KEY=doctor-placeholder",
    "MCP_URL_TOKEN=doctor-placeholder",
    "VOICE_TURN_SHARED_SECRET=doctor-placeholder",
    "VOICE_TURN_HOSTNAME=localhost"
  ), [Text.Encoding]::ASCII)
  docker compose --env-file $composeVars --profile gpu config --quiet
  Pass "compose-config" "valid with isolated placeholder values"
} catch { Fail "compose-config" $_ }
finally { if(Test-Path -LiteralPath $composeVars) { Remove-Item -LiteralPath $composeVars -Force } }

try {
  $agent = Invoke-RestMethod -Uri "http://127.0.0.1:8788/health" -TimeoutSec 3
  if($agent.status -eq "ok") {
    Pass "host-agent" ("v" + $agent.version + "; Tailscale=" + $agent.tailscale + "; HF CLI=" + $agent.hf_cli)
  } else { Fail "host-agent" ("unexpected status: " + $agent.status) }
} catch { Fail "host-agent" $_ }

$startupAgent = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\LocalAIHostAgent.cmd"
if(Test-Path $startupAgent) { Pass "host-agent-autostart" "Startup entry present" }
else { Fail "host-agent-autostart" "Startup entry missing" }

$running = @(docker ps --format "{{.Names}}" | Where-Object { $_ -match "^ai-stack-(llm|reasoning|stt|tts|vlm|comfyui|wangp|lerobot)$" })
if($running.Count -le 1) { Pass "gpu-exclusivity" ($running -join ",") }
else { Fail "gpu-exclusivity" ($running -join ",") }

$visualMemoryState = $null
try { $visualMemoryState = (& docker inspect ai-stack-visual-memory --format "{{.State.Status}}" 2>$null) }
catch { $visualMemoryState = $null }
if($visualMemoryState -eq "running") {
  Pass "visual-memory-container" "running"
  try {
    $visualMemoryJson = & docker exec ai-stack-visual-memory python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/diagnostics', timeout=3).read().decode())"
    $visualMemory = $visualMemoryJson | ConvertFrom-Json
    $modelSummary = $visualMemory.model + "@" + $visualMemory.revision + "; device=" + $visualMemory.device + "; loaded=" + $visualMemory.loaded
    if($visualMemory.model_files_available) { Pass "visual-memory-model" $modelSummary }
    else { Fail "visual-memory-model" ($modelSummary + "; local files missing") }
    if($visualMemory.database.available -and $visualMemory.database.writable) {
      Pass "visual-memory-database" ("writable; records=" + $visualMemory.database.records + "; observations=" + $visualMemory.database.observations + "; vectors=" + $visualMemory.database.vectors)
    } else {
      Fail "visual-memory-database" ("available=" + $visualMemory.database.available + "; writable=" + $visualMemory.database.writable)
    }
    if($visualMemory.index.compatible) { Pass "visual-memory-index" ($visualMemory.index.backend + "; " + $visualMemory.index.size_bytes + " bytes") }
    else { Warn "visual-memory-index" "index is absent or incompatible and will need a rebuild before retrieval" }
  } catch { Fail "visual-memory-diagnostics" $_ }
} else {
  Warn "visual-memory-container" ("state=" + ($visualMemoryState -join ""))
}

try {
  $supervisorInfo = @((& docker inspect ai-stack-supervisor) | ConvertFrom-Json)[0]
  $controlInfo = @((& docker inspect ai-stack-docker-control) | ConvertFrom-Json)[0]
  $supervisorSocket = @($supervisorInfo.Mounts | Where-Object { $_.Destination -eq "/var/run/docker.sock" }).Count -gt 0
  $controlSocket = @($controlInfo.Mounts | Where-Object { $_.Destination -eq "/var/run/docker.sock" }).Count -gt 0
  $controlInternal = ((& docker network inspect ai-stack-docker-control-net --format "{{.Internal}}") -join "").Trim().ToLowerInvariant() -eq "true"
  if(-not $supervisorSocket -and $controlSocket -and $controlInternal) {
    Pass "docker-socket-isolation" "socket restricted to docker-control on internal network"
  } else {
    Fail "docker-socket-isolation" ("supervisorSocket=$supervisorSocket; controlSocket=$controlSocket; internalNetwork=$controlInternal")
  }
} catch { Fail "docker-socket-isolation" $_ }

foreach($svc in @("docker-control","telemetry","supervisor","gateway","eval-router","dashboard","mcp","agent-lab","stt","vlm","visual-memory","comfyui","wangp")) {
  $container = "ai-stack-$svc"
  try {
    $containerImage = & docker inspect $container --format "{{.Image}}" 2>$null
    $latestImage = & docker image inspect ("ai-stack-" + $svc + ":latest") --format "{{.Id}}" 2>$null
  } catch { continue }
  if(-not $containerImage -or -not $latestImage) { continue }
  if($containerImage -eq $latestImage) { Pass ("image-sync:" + $svc) "current" }
  else { Fail ("image-sync:" + $svc) "container uses an older image; recreate it" }
}

try {
  $sandboxInfo = @((& docker inspect ai-stack-agent-lab-sandbox) | ConvertFrom-Json)[0]
  $sandboxImage = $sandboxInfo.Image
  $agentLabImage = (& docker image inspect "ai-stack-agent-lab:latest" --format "{{.Id}}" 2>$null)
  $runsMount = @($sandboxInfo.Mounts | Where-Object { $_.Destination -eq "/runs" -and -not $_.RW }).Count -eq 1
  $jobsMount = @($sandboxInfo.Mounts | Where-Object { $_.Destination -eq "/jobs" -and $_.RW }).Count -eq 1
  $networkless = $sandboxInfo.HostConfig.NetworkMode -eq "none"
  $noSecret = @($sandboxInfo.Config.Env | Where-Object { $_ -like "AI_API_KEY=*" -or $_ -like "SUPERVISOR_TOKEN=*" -or $_ -like "LLAMA_API_KEY=*" }).Count -eq 0
  if($sandboxImage -eq $agentLabImage -and $runsMount -and $jobsMount -and $networkless -and $noSecret) {
    Pass "agent-lab-sandbox-isolation" "current image; network=none; runs=ro; no AI credentials"
  } else {
    Fail "agent-lab-sandbox-isolation" ("imageCurrent=" + ($sandboxImage -eq $agentLabImage) + "; runsRO=$runsMount; jobsRW=$jobsMount; networkNone=$networkless; noSecrets=$noSecret")
  }
} catch { Fail "agent-lab-sandbox-isolation" $_ }

foreach($svc in @("docker-control","supervisor","telemetry","gateway","eval-router","dashboard","mcp","agent-lab","stt","vlm","visual-memory")) {
  $currentHash = (& python (Join-Path $PSScriptRoot "source_hash.py") $svc).Trim()
  try { $imageJson = & docker image inspect ("ai-stack-" + $svc + ":latest") 2>$null }
  catch { $imageJson = $null }
  $builtHash = $null
  if($imageJson) {
    $imageInfo = @($imageJson | ConvertFrom-Json)[0]
    if($imageInfo.Config.Labels) {
      $builtHash = $imageInfo.Config.Labels.'io.ai-stack.source-hash'
    }
  }
  if(-not $builtHash -or $builtHash -eq "unknown") {
    Fail ("source-sync:" + $svc) "image has no source fingerprint; rebuild it"
  } elseif($builtHash -eq $currentHash) {
    Pass ("source-sync:" + $svc) "image matches current source"
  } else {
    Fail ("source-sync:" + $svc) "source changed since image build"
  }
}

try {
  $proxy = Invoke-RestMethod -Uri "http://127.0.0.1:3000/api/health" -TimeoutSec 4
  if($proxy.service -eq "gateway") { Pass "dashboard-proxy" ("gateway v" + $proxy.version) }
  else { Fail "dashboard-proxy" ("unexpected backend: " + ($proxy | ConvertTo-Json -Compress)) }
} catch { Fail "dashboard-proxy" $_ }

try {
  $lab = Invoke-RestMethod -Uri "http://127.0.0.1:8770/health" -TimeoutSec 4
  if($lab.service -eq "agent-lab" -and $lab.status -eq "ok" -and $lab.sandbox -eq "ok") {
    Pass "agent-lab" ("v" + $lab.version + "; sandbox=ok; activeRuns=" + @($lab.active_runs).Count)
  } else {
    Fail "agent-lab" ("unexpected response: " + ($lab | ConvertTo-Json -Compress))
  }
} catch { Fail "agent-lab" $_ }

try {
  $bench = Invoke-RestMethod -Uri "http://127.0.0.1:8770/benchmarks/reference" -TimeoutSec 4
  if($bench.suite -eq "agent-lab-core-v2" -and $bench.cases -ge 13 -and $bench.failed -eq 0 -and $bench.passed -eq $bench.cases) {
    Pass "agent-lab-benchmark-reference" ($bench.model + "; " + $bench.passed + "/" + $bench.cases + " passed")
  } else {
    Fail "agent-lab-benchmark-reference" ("unexpected reference: " + ($bench | Select-Object suite,model,cases,passed,failed | ConvertTo-Json -Compress))
  }
} catch { Fail "agent-lab-benchmark-reference" $_ }

$drives=Get-PSDrive C,D -ErrorAction SilentlyContinue
foreach($d in $drives) { Pass ("disk:"+$d.Name) (([math]::Round($d.Free/1GB,1)).ToString()+" GiB free") }

if($Failures) { throw "$Failures doctor check(s) failed" }
Write-Output "ALL_DOCTOR_CHECKS_PASSED"
