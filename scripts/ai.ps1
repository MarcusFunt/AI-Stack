#Requires -Version 5.1
[CmdletBinding()]
param(
  [Parameter(Position=0)]
  [ValidateSet("start","stop","stop-all","status","build","create","update","rollback","doctor","model-info","smoke","test-leases","test-proxy","burn-in","bench","logs","down")]
  [string]$Action = "status",
  [Parameter(Position=1)]
  [ValidateSet("gateway","llm","reasoning","stt","tts","vlm","comfyui","wangp","lerobot")]
  [string]$Service = "gateway",
  [Parameter(Position=2)]
  [string]$Snapshot = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$GpuServices = @("llm","reasoning","stt","tts","vlm","comfyui","wangp","lerobot")
$BuildHashServices = @("docker-control","supervisor","telemetry","gateway","dashboard","mcp","stt","vlm")
Set-Location $Root

function Set-BuildSourceHashes {
  foreach($svc in $BuildHashServices) {
    $hash = (& python (Join-Path $PSScriptRoot "source_hash.py") $svc).Trim()
    if($LASTEXITCODE -ne 0 -or -not $hash) { throw "failed to compute source hash for $svc" }
    $envName = "AI_STACK_" + $svc.ToUpperInvariant().Replace("-", "_") + "_SOURCE_HASH"
    [Environment]::SetEnvironmentVariable($envName, $hash, "Process")
  }
}

function Invoke-Compose {
  param([Parameter(Mandatory=$true)][string[]]$CommandArgs)
  & docker compose @CommandArgs
  if ($LASTEXITCODE -ne 0) { throw "docker compose failed: $($CommandArgs -join ' ')" }
}

function Wait-Gateway {
  for($i=0; $i -lt 40; $i++) {
    try {
      $r = Invoke-RestMethod http://127.0.0.1:8090/health -TimeoutSec 2
      if($r.status -in @("ok","degraded")) { return }
    } catch {}
    Start-Sleep -Milliseconds 750
  }
  throw "gateway did not become ready"
}

function Test-HostAgent {
  try {
    $r = Invoke-RestMethod "http://127.0.0.1:8788/health" -TimeoutSec 2
    return $r.status -eq "ok"
  } catch { return $false }
}

function Start-HostAgent {
  if (Test-HostAgent) { return }

  # A crashed/stale pythonw process should not prevent recovery.
  $stale = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -like "*AI-Stack*host_agent.py*"
  })
  foreach($proc in $stale) {
    try { Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop } catch {}
  }

  $launcher = Join-Path $PSScriptRoot "start-host-agent.cmd"
  if (Test-Path $launcher) {
    Start-Process -FilePath "cmd.exe" -ArgumentList @("/d","/s","/c",('"' + $launcher + '"')) -WorkingDirectory $Root -WindowStyle Hidden
  } else {
    $script = Join-Path $PSScriptRoot "host_agent.py"
    $outLog = Join-Path $Root "data\state\host-agent.log"
    $errLog = Join-Path $Root "data\state\host-agent-error.log"
    Start-Process -FilePath "pythonw.exe" -ArgumentList @($script) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog
  }

  for($i=0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 250
    if (Test-HostAgent) { return }
  }
  throw "Windows host agent did not become ready on port 8788"
}

function Start-ControlPlane {
  Start-HostAgent
  Invoke-Compose -CommandArgs @("up","-d","telemetry","supervisor","gateway","dashboard","mcp")
  Wait-Gateway
}

function Invoke-Supervisor {
  param([string]$Method,[string]$Path,[int]$Timeout=600)
  $url = "http://127.0.0.1:8000$Path"
  $code = "import os,urllib.request; h={'X-Supervisor-Token':os.environ['SUPERVISOR_TOKEN']}; r=urllib.request.urlopen(urllib.request.Request('$url',headers=h,method='$Method'),timeout=$Timeout); print(r.read().decode())"
  & docker exec ai-stack-supervisor python -c $code
  if ($LASTEXITCODE -ne 0) { throw "supervisor request failed: $Method $Path" }
}

function Stop-GpuFallback {
  foreach ($item in $GpuServices) {
    $name = "ai-stack-$item"
    $running = & docker ps -q --filter "name=^/$name$"
    if ($running) { & docker stop --time 30 $name | Out-Null }
  }
}

function Assert-CleanTrackedRepo {
  param([string]$Path)
  $dirty = & git -C $Path status --porcelain --untracked-files=no
  if ($LASTEXITCODE -ne 0) { throw "git status failed for $Path" }
  if ($dirty) { throw "tracked changes in $Path; refusing update/rollback" }
}

function Assert-NoActiveJobs {
  Start-ControlPlane | Out-Null
  $raw = Invoke-Supervisor "GET" "/status" 10
  $state = $raw | ConvertFrom-Json
  $busy = @($state.active_jobs.PSObject.Properties | Where-Object { [int]$_.Value -gt 0 })
  if($busy.Count -gt 0) {
    $detail = ($busy | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ", "
    throw "active AI jobs prevent maintenance: $detail"
  }
}

function Save-RollbackImage {
  param([string]$Source,[string]$Tag)
  $id = (& docker images -q $Source | Select-Object -First 1)
  if ($id) {
    & docker tag $Source $Tag
    if ($LASTEXITCODE -ne 0) { throw "failed to tag $Source" }
    return $Tag
  }
  return $null
}

switch ($Action) {
  "start" {
    Start-ControlPlane
    if ($Service -ne "gateway") { Invoke-Supervisor "POST" "/ensure/$Service" }
  }
  "stop" {
    if ($Service -eq "gateway") { Invoke-Compose -CommandArgs @("stop","gateway") }
    else {
      Start-ControlPlane
      Invoke-Supervisor "POST" "/stop/$Service" 120
    }
  }
  "stop-all" {
    try {
      Start-ControlPlane
    }
    catch {
      Write-Warning "Control plane unavailable; using direct GPU-container fallback."
      Stop-GpuFallback
      break
    }
    Invoke-Supervisor "POST" "/stop-all" 180
  }
  "status" {
    Invoke-Compose -CommandArgs @("--profile","gpu","ps","-a")
    try { Invoke-Supervisor "GET" "/status" 10 } catch { Write-Warning $_ }
  }
  "build" {
    Set-BuildSourceHashes
    Invoke-Compose -CommandArgs @("build","docker-control","telemetry","supervisor","gateway","dashboard","mcp","stt","vlm","comfyui","wangp")
  }
  "create" {
    Set-BuildSourceHashes
    Invoke-Compose -CommandArgs @("build","docker-control","telemetry","supervisor","gateway","dashboard","mcp","stt","vlm","comfyui","wangp")
    Invoke-Compose -CommandArgs @("pull","llm","reasoning")
    Invoke-Compose -CommandArgs @("--profile","gpu","create","--force-recreate","llm","reasoning","stt","tts","vlm","comfyui","wangp")
    Start-ControlPlane
  }
  "update" {
    Assert-NoActiveJobs
    Assert-CleanTrackedRepo (Join-Path $Root "third_party\ComfyUI")
    Assert-CleanTrackedRepo (Join-Path $Root "third_party\Wan2GP")
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"

    $rollbackImages = [ordered]@{}
    foreach($svc in @("docker-control","telemetry","supervisor","gateway","dashboard","mcp","stt","vlm","comfyui","wangp")) {
      $tag = Save-RollbackImage ("ai-stack-" + $svc + ":latest") ("ai-stack-" + $svc + ":rollback-" + $stamp)
      if($tag) { $rollbackImages[$svc] = $tag }
    }
    $llamaRollback = Save-RollbackImage "ghcr.io/ggml-org/llama.cpp:server-cuda" ("ai-stack-llama:rollback-" + $stamp)

    $state = [ordered]@{
      timestamp = $stamp
      comfyui = [ordered]@{
        sha = (& git -C (Join-Path $Root "third_party\ComfyUI") rev-parse HEAD)
        branch = (& git -C (Join-Path $Root "third_party\ComfyUI") branch --show-current)
      }
      wangp = [ordered]@{
        sha = (& git -C (Join-Path $Root "third_party\Wan2GP") rev-parse HEAD)
        branch = (& git -C (Join-Path $Root "third_party\Wan2GP") branch --show-current)
      }
      rollback_images = $rollbackImages
      llama_rollback = $llamaRollback
    }
    $statePath = Join-Path $Root ("data\state\update-" + $stamp + ".json")
    $state | ConvertTo-Json -Depth 8 | Set-Content $statePath -Encoding UTF8

    & git -C (Join-Path $Root "third_party\ComfyUI") pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "ComfyUI update failed" }
    & git -C (Join-Path $Root "third_party\Wan2GP") pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "Wan2GP update failed" }

    Invoke-Compose -CommandArgs @("pull","llm","reasoning")
    Set-BuildSourceHashes
    Invoke-Compose -CommandArgs @("build","docker-control","telemetry","supervisor","gateway","dashboard","mcp","stt","vlm","comfyui","wangp")
    Invoke-Compose -CommandArgs @("--profile","gpu","create","--force-recreate","llm","reasoning","stt","tts","vlm","comfyui","wangp")
    Invoke-Compose -CommandArgs @("up","-d","--force-recreate","docker-control","telemetry","supervisor","gateway","dashboard","mcp")
    Write-Output "Update complete. Rollback snapshot: $statePath"
  }
  "rollback" {
    Assert-NoActiveJobs
    if(-not $Snapshot) {
      $latest = Get-ChildItem (Join-Path $Root "data\state\update-*.json") |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
      if(-not $latest) { throw "no update snapshot found" }
      $Snapshot = $latest.FullName
    }
    $state = Get-Content $Snapshot -Raw | ConvertFrom-Json
    Assert-CleanTrackedRepo (Join-Path $Root "third_party\ComfyUI")
    Assert-CleanTrackedRepo (Join-Path $Root "third_party\Wan2GP")

    & git -C (Join-Path $Root "third_party\ComfyUI") reset --hard $state.comfyui.sha
    if ($LASTEXITCODE -ne 0) { throw "ComfyUI rollback failed" }
    & git -C (Join-Path $Root "third_party\Wan2GP") reset --hard $state.wangp.sha
    if ($LASTEXITCODE -ne 0) { throw "Wan2GP rollback failed" }

    if($state.rollback_images) {
      foreach($prop in $state.rollback_images.PSObject.Properties) {
        & docker tag $prop.Value ("ai-stack-" + $prop.Name + ":latest")
        if ($LASTEXITCODE -ne 0) { throw "failed to restore image for $($prop.Name)" }
      }
    }
    if($state.llama_rollback) {
      & docker tag $state.llama_rollback "ghcr.io/ggml-org/llama.cpp:server-cuda"
      if ($LASTEXITCODE -ne 0) { throw "failed to restore llama.cpp image" }
    }

    Invoke-Compose -CommandArgs @("--profile","gpu","create","--force-recreate","llm","reasoning","stt","tts","vlm","comfyui","wangp")
    Invoke-Compose -CommandArgs @("up","-d","--force-recreate","docker-control","telemetry","supervisor","gateway","dashboard","mcp")
    Write-Output "Rollback complete from: $Snapshot"
  }
  "doctor" { & (Join-Path $PSScriptRoot "doctor.ps1") }
  "model-info" { & python (Join-Path $PSScriptRoot "gguf-info.py") "models\llm\daily.gguf" "models\llm\reasoning.gguf" }
  "smoke" { & (Join-Path $PSScriptRoot "smoke.ps1") }
  "test-leases" {
    Start-ControlPlane
    try {
      & python (Join-Path $PSScriptRoot "lease-test.py")
      if ($LASTEXITCODE -ne 0) { throw "lease regression test failed" }
    }
    finally {
      & $PSCommandPath stop-all
    }
  }
  "test-proxy" {
    Start-ControlPlane
    & (Join-Path $PSScriptRoot "proxy-regression.ps1")
  }
  "burn-in" {
    & (Join-Path $PSScriptRoot "burn-in.ps1")
    if ($LASTEXITCODE -ne 0) { throw "burn-in suite failed" }
  }
  "bench" { & (Join-Path $PSScriptRoot "benchmark-llm.ps1") -Service $Service }
  "logs" { & docker logs --tail 200 "ai-stack-$Service" }
  "down" { Invoke-Compose -CommandArgs @("--profile","gpu","down") }
}
