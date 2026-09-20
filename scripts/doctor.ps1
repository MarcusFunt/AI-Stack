#Requires -Version 5.1
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Pass($name,$detail) { Write-Output ("[PASS] " + $name + ": " + $detail) }
function Fail($name,$detail) { Write-Output ("[FAIL] " + $name + ": " + $detail); $script:Failures++ }
$script:Failures = 0

try { $dv = docker --version; Pass "docker" $dv } catch { Fail "docker" $_ }
try { $cv = docker compose version; Pass "compose" $cv } catch { Fail "compose" $_ }
try {
  $gpu = nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
  Pass "gpu" $gpu
} catch { Fail "gpu" $_ }

foreach($key in @("AI_API_KEY","SUPERVISOR_TOKEN","LLAMA_API_KEY","HOST_AGENT_TOKEN","LLM_MODEL","REASONING_MODEL")) {
  if(Get-Content .env | Where-Object { $_ -like "$key=*" }) { Pass "env:$key" "present" }
  else { Fail "env:$key" "missing" }
}

foreach($file in @("models\llm\daily.gguf","models\llm\reasoning.gguf","config\models.json")) {
  if(Test-Path $file) {
    $item=Get-Item $file
    Pass "file:$file" (([math]::Round($item.Length/1GB,3)).ToString()+" GiB")
  } else { Fail "file:$file" "missing" }
}

try { docker compose --profile gpu config | Out-Null; Pass "compose-config" "valid" }
catch { Fail "compose-config" $_ }

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

foreach($svc in @("docker-control","telemetry","supervisor","gateway","dashboard","mcp","stt","vlm","comfyui","wangp")) {
  $container = "ai-stack-$svc"
  $containerImage = & docker inspect $container --format "{{.Image}}" 2>$null
  $latestImage = & docker image inspect ("ai-stack-" + $svc + ":latest") --format "{{.Id}}" 2>$null
  if(-not $containerImage -or -not $latestImage) { continue }
  if($containerImage -eq $latestImage) { Pass ("image-sync:" + $svc) "current" }
  else { Fail ("image-sync:" + $svc) "container uses an older image; recreate it" }
}

foreach($svc in @("docker-control","supervisor","telemetry","gateway","dashboard","mcp","stt","vlm")) {
  $currentHash = (& python (Join-Path $PSScriptRoot "source_hash.py") $svc).Trim()
  $imageJson = & docker image inspect ("ai-stack-" + $svc + ":latest") 2>$null
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

$drives=Get-PSDrive C,D -ErrorAction SilentlyContinue
foreach($d in $drives) { Pass ("disk:"+$d.Name) (([math]::Round($d.Free/1GB,1)).ToString()+" GiB free") }

if($Failures) { throw "$Failures doctor check(s) failed" }
Write-Output "ALL_DOCTOR_CHECKS_PASSED"
