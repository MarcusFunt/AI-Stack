#Requires -Version 5.1
[CmdletBinding()]
param(
  [Parameter(Position=0)]
  [ValidateSet("launch","start","stop","stop-all","status","build","create","update","rollback","doctor","model-info","smoke","voice-smoke","test-leases","test-proxy","test-agent-lab","test-agent-evaluator","bench-agent-lab","burn-in","bench","logs","down")]
  [string]$Action = "status",
  [Parameter(Position=1)]
  [ValidateSet("gateway","voice","coturn","visual-memory","llm","reasoning","stt","tts","vlm","comfyui","wangp","lerobot")]
  [string]$Service = "gateway",
  [Parameter(Position=2)]
  [string]$Snapshot = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$GpuServices = @("llm","reasoning","stt","tts","vlm","comfyui","wangp","lerobot")
$BuildHashServices = @("docker-control","supervisor","telemetry","gateway","voice","eval-router","dashboard","mcp","agent-lab","agent-evaluator","agent-eval-runner","stt","vlm","visual-memory")
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

function Test-DockerEngine {
  try {
    $null = & docker info --format "{{.ServerVersion}}" 2>$null
    return $LASTEXITCODE -eq 0
  } catch { return $false }
}

function Ensure-DockerEngine {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    foreach ($candidate in @(
      "$env:ProgramFiles\Docker\Docker\resources\bin\docker.exe",
      "${env:ProgramFiles(x86)}\Docker\Docker\resources\bin\docker.exe"
    )) {
      if ($candidate -and (Test-Path $candidate)) {
        $env:PATH = (Split-Path $candidate -Parent) + ";" + $env:PATH
        break
      }
    }
  }
  if (Test-DockerEngine) { Write-Output "Docker Engine is ready."; return }
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI was not found. Install Docker Desktop, then run this launcher again."
  }

  $desktopPaths = @(@(
    (Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Docker\Docker\Docker Desktop.exe")
  ) | Where-Object { $_ -and (Test-Path $_) })
  if (-not $desktopPaths) {
    throw "Docker Engine is not running and Docker Desktop was not found. Start or install Docker Desktop, then try again."
  }

  Write-Output "Starting Docker Desktop and waiting for its engine..."
  Start-Process -FilePath $desktopPaths[0] -WindowStyle Minimized
  for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 2
    if (Test-DockerEngine) { Write-Output "Docker Engine is ready."; return }
    if (($i + 1) % 15 -eq 0) { Write-Output "Still waiting for Docker Desktop..." }
  }
  throw "Docker Desktop did not make its engine ready within 3 minutes. Check Docker Desktop, then run the launcher again."
}

function Start-TailscaleBestEffort {
  $tailscale = Get-Command tailscale.exe -ErrorAction SilentlyContinue
  $tailscalePath = $null
  if ($tailscale) {
    $tailscalePath = $tailscale.Path
    if (-not $tailscalePath) { $tailscalePath = $tailscale.Source }
    if (-not $tailscalePath -and $tailscale.CommandType -eq "Function") { $tailscalePath = $tailscale.Name }
  }
  if (-not $tailscalePath) {
    foreach ($candidate in @("$env:ProgramFiles\Tailscale\tailscale.exe", "${env:ProgramFiles(x86)}\Tailscale\tailscale.exe")) {
      if ($candidate -and (Test-Path $candidate)) { $tailscalePath = $candidate; break }
    }
  }
  if (-not $tailscalePath) { Write-Warning "Tailscale is not installed; continuing with local-only startup."; return }

  try {
    $statusText = & $tailscalePath status --json 2>$null
    if ($LASTEXITCODE -eq 0) {
      $status = ($statusText -join "`n") | ConvertFrom-Json
      if ($status.BackendState -eq "Running" -and $status.Self.Online) {
        Write-Output "Tailscale is online. Existing Serve/Funnel routes were left unchanged."
        return
      }
    }

    $service = Get-Service -Name "Tailscale" -ErrorAction SilentlyContinue
    if ($service -and $service.Status -ne "Running") {
      try { Start-Service -Name "Tailscale" -ErrorAction Stop } catch { Write-Warning "Could not start the Tailscale service: $($_.Exception.Message)" }
    }
    Start-Sleep -Seconds 2
    $statusText = & $tailscalePath status --json 2>$null
    if ($LASTEXITCODE -eq 0) {
      $status = ($statusText -join "`n") | ConvertFrom-Json
      if ($status.BackendState -eq "Running" -and $status.Self.Online) {
        Write-Output "Tailscale is online. Existing Serve/Funnel routes were left unchanged."
        return
      }
    }
    Write-Warning "Tailscale is installed but not online. Local startup will continue; sign in or reconnect in Tailscale if remote access is needed."
  } catch {
    Write-Warning "Could not verify Tailscale status: $($_.Exception.Message). Local startup will continue."
  }
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

function Wait-Dashboard {
  for($i=0; $i -lt 60; $i++) {
    try {
      $r = Invoke-RestMethod http://127.0.0.1:3000/api/health -TimeoutSec 2
      if($r.service -eq "gateway") { return }
    } catch {}
    Start-Sleep -Seconds 1
  }
  throw "dashboard did not become ready at http://127.0.0.1:3000"
}

function Get-TailnetDnsName {
  $code = 'import json,os,urllib.request; request=urllib.request.Request("http://127.0.0.1:8000/control/network",headers={"Authorization":"Bearer "+os.environ["AI_API_KEY"]}); print(json.load(urllib.request.urlopen(request,timeout=10)).get("dns_name",""))'
  $dnsName = (& docker exec ai-stack-gateway python -c $code).Trim()
  if($LASTEXITCODE -ne 0 -or -not $dnsName) { throw "could not resolve the authenticated Tailnet DNS name" }
  return $dnsName.TrimEnd(".")
}

function Test-HostAgent {
  try {
    $source = [IO.File]::ReadAllText((Join-Path $PSScriptRoot "host_agent.py"))
    $versionMatch = [regex]::Match($source, 'HOST_AGENT_VERSION\s*=\s*"([^"]+)"')
    if(-not $versionMatch.Success) { return $false }
    $r = Invoke-RestMethod "http://127.0.0.1:8788/health" -TimeoutSec 2
    return $r.status -eq "ok" -and $r.version -eq $versionMatch.Groups[1].Value
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
  Set-BuildSourceHashes
  Invoke-Compose -CommandArgs @("up","-d","telemetry","supervisor","gateway","voice","eval-router","dashboard","mcp","agent-lab-sandbox","agent-eval-runner","agent-evaluator","agent-lab")
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
  $dirty = & git -C $Path status --porcelain --untracked-files=all
  if ($LASTEXITCODE -ne 0) { throw "git status failed for $Path" }
  if ($dirty) { throw "changes in $Path; refusing update/rollback" }
}

function Get-RootSourceUpdate {
  $previousPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & git -C $Root fetch origin refs/heads/main:refs/remotes/origin/main 2>&1 | Out-Null
    $fetchExit = $LASTEXITCODE
  } finally { $ErrorActionPreference = $previousPreference }
  if ($fetchExit -ne 0) { throw "fetch from origin/main failed; source was not changed" }
  $previous = (& git -C $Root rev-parse HEAD).Trim()
  if ($LASTEXITCODE -ne 0 -or -not $previous) { throw "could not read the current AI-Stack source revision" }
  $target = (& git -C $Root rev-parse origin/main).Trim()
  if ($LASTEXITCODE -ne 0 -or -not $target) { throw "could not read origin/main after fetch" }
  $branch = (& git -C $Root branch --show-current).Trim()
  if ($LASTEXITCODE -ne 0 -or -not $branch) { throw "AI-Stack must be on a named branch before updating" }
  $previousPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    & git -C $Root merge-base --is-ancestor $previous $target 2>&1 | Out-Null
    $ancestorExit = $LASTEXITCODE
  } finally { $ErrorActionPreference = $previousPreference }
  if ($ancestorExit -ne 0) { throw "current AI-Stack branch cannot fast-forward to origin/main; no branch was changed" }
  return [pscustomobject]@{ previous_sha=$previous; updated_sha=$target; branch=$branch }
}

function Assert-RootWorktreeClean {
  $dirty = & git -C $Root status --porcelain --untracked-files=all
  if ($LASTEXITCODE -ne 0) { throw "git status failed for AI-Stack" }
  if ($dirty) { throw "AI-Stack worktree has tracked or untracked changes; refusing update/rollback" }
}

function New-SourceRevisionSnapshot {
  param([Parameter(Mandatory=$true)][object]$SourceUpdate)
  return [ordered]@{
    previous_sha = [string]$SourceUpdate.previous_sha
    updated_sha = [string]$SourceUpdate.updated_sha
    branch = [string]$SourceUpdate.branch
  }
}

function Assert-SourceRollbackSafe {
  param(
    [Parameter(Mandatory=$true)][object]$Snapshot,
    [Parameter(Mandatory=$true)][string]$CurrentBranch,
    [Parameter(Mandatory=$true)][string]$CurrentSha
  )
  if ($CurrentBranch -ne $Snapshot.branch -or $CurrentSha -notin @($Snapshot.previous_sha, $Snapshot.updated_sha)) {
    throw "AI-Stack source no longer matches this snapshot; refusing guarded source rollback"
  }
}

function Assert-NoActiveJobs {
  Start-ControlPlane | Out-Null
  Assert-CurrentSupervisorIdle
}

function Assert-CurrentSupervisorIdle {
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
  "launch" {
    Ensure-DockerEngine
    Start-TailscaleBestEffort
    Start-ControlPlane
    Wait-Dashboard
    Start-Process "http://127.0.0.1:3000"
    Write-Output "AI-Stack is ready at http://127.0.0.1:3000."
  }
  "start" {
    if ($Service -ne "coturn") { Start-ControlPlane }
    if ($Service -eq "visual-memory") {
      Assert-NoActiveJobs
      $expectedSourceHash = (& python (Join-Path $PSScriptRoot "source_hash.py") "visual-memory").Trim()
      if($LASTEXITCODE -ne 0 -or -not $expectedSourceHash) { throw "failed to compute visual-memory source hash" }
      $imageInspect = @(& docker image inspect ai-stack-visual-memory:latest 2>$null | ConvertFrom-Json)
      $imageSourceHash = ""
      if($LASTEXITCODE -eq 0 -and $imageInspect) {
        $imageSourceHash = [string]$imageInspect[0].Config.Labels."io.ai-stack.source-hash"
      }
      if($imageSourceHash -ne $expectedSourceHash) {
        Invoke-Compose -CommandArgs @("build","visual-memory")
      }
      Assert-CurrentSupervisorIdle
      Invoke-Compose -CommandArgs @("restart","docker-control","supervisor")
      $supervisorReady = $false
      for($i=0; $i -lt 30; $i++) {
        try {
          Invoke-Supervisor "GET" "/status" 10 2>$null | Out-Null
          $supervisorReady = $true
          break
        } catch { Start-Sleep -Seconds 1 }
      }
      if(-not $supervisorReady) { throw "supervisor did not become ready after restart" }
      Invoke-Compose -CommandArgs @("up","-d","visual-memory")
    }
    elseif ($Service -eq "voice") { Invoke-Compose -CommandArgs @("up","-d","voice") }
    elseif ($Service -eq "coturn") {
      $turnProxyEnabled = "0"
      $turnRestartPolicy = "no"
      $previousTurnHostname = $env:VOICE_TURN_HOSTNAME
      if ($env:VOICE_TURN_SHARED_SECRET) {
        $turnProxyEnabled = "1"
        $turnRestartPolicy = "unless-stopped"
        $env:VOICE_TURN_HOSTNAME = Get-TailnetDnsName
      }
      $previousTurnRestartPolicy = $env:TURN_RESTART_POLICY
      [Environment]::SetEnvironmentVariable("TURN_PROXY_ENABLED", $turnProxyEnabled, "Process")
      [Environment]::SetEnvironmentVariable("TURN_RESTART_POLICY", $turnRestartPolicy, "Process")
      try {
        Invoke-Compose -CommandArgs @("up","--build","-d","coturn","turn-proxy")
        if ($env:VOICE_TURN_SHARED_SECRET) {
          # Apply the same runtime ICE settings to voice; Compose recreates it only if config changed.
          Invoke-Compose -CommandArgs @("up","-d","voice")
        }
      }
      finally {
        [Environment]::SetEnvironmentVariable("TURN_PROXY_ENABLED", $null, "Process")
        [Environment]::SetEnvironmentVariable("TURN_RESTART_POLICY", $previousTurnRestartPolicy, "Process")
        [Environment]::SetEnvironmentVariable("VOICE_TURN_HOSTNAME", $previousTurnHostname, "Process")
      }
    }
    elseif ($Service -ne "gateway") { Invoke-Supervisor "POST" "/ensure/$Service" }
  }
  "stop" {
    if ($Service -eq "coturn") { Invoke-Compose -CommandArgs @("stop","coturn","turn-proxy") }
    elseif ($Service -in @("gateway","voice")) { Invoke-Compose -CommandArgs @("stop",$Service) }
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
    Invoke-Compose -CommandArgs @("build","docker-control","telemetry","supervisor","gateway","voice","eval-router","dashboard","mcp","agent-lab","agent-evaluator","agent-eval-runner","stt","tts","vlm","comfyui","wangp","visual-memory")
  }
  "create" {
    Set-BuildSourceHashes
    if ($Service -in $GpuServices) {
      Assert-NoActiveJobs
      Invoke-Compose -CommandArgs @("build",$Service)
      Invoke-Compose -CommandArgs @("--profile","gpu","create","--force-recreate",$Service)
      Start-ControlPlane
    } else {
      Invoke-Compose -CommandArgs @("build","docker-control","telemetry","supervisor","gateway","voice","eval-router","dashboard","mcp","agent-lab","agent-evaluator","agent-eval-runner","stt","tts","vlm","comfyui","wangp")
      Invoke-Compose -CommandArgs @("pull","llm","reasoning")
      Invoke-Compose -CommandArgs @("--profile","gpu","create","--force-recreate","llm","reasoning","stt","tts","vlm","comfyui","wangp")
      Start-ControlPlane
    }
  }
  "update" {
    Assert-RootWorktreeClean
    Assert-NoActiveJobs
    Assert-RootWorktreeClean
    Assert-CleanTrackedRepo (Join-Path $Root "third_party\ComfyUI")
    Assert-CleanTrackedRepo (Join-Path $Root "third_party\Wan2GP")
    $sourceUpdate = Get-RootSourceUpdate
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"

    $rollbackImages = [ordered]@{}
    foreach($svc in @("docker-control","telemetry","supervisor","gateway","voice","eval-router","dashboard","mcp","agent-lab","agent-evaluator","agent-eval-runner","stt","vlm","comfyui","wangp")) {
      $tag = Save-RollbackImage ("ai-stack-" + $svc + ":latest") ("ai-stack-" + $svc + ":rollback-" + $stamp)
      if($tag) { $rollbackImages[$svc] = $tag }
    }
    $llamaRollback = Save-RollbackImage "ghcr.io/ggml-org/llama.cpp:server-cuda" ("ai-stack-llama:rollback-" + $stamp)

    $state = [ordered]@{
      timestamp = $stamp
      ai_stack = (New-SourceRevisionSnapshot -SourceUpdate $sourceUpdate)
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

    if ($sourceUpdate.previous_sha -ne $sourceUpdate.updated_sha) {
      $previousPreference = $ErrorActionPreference
      $ErrorActionPreference = "Continue"
      try {
        & git -C $Root merge --ff-only origin/main 2>&1 | Out-Null
        $mergeExit = $LASTEXITCODE
      } finally { $ErrorActionPreference = $previousPreference }
      if ($mergeExit -ne 0) { throw "AI-Stack source fast-forward failed; rollback snapshot: $statePath" }
    }

    & git -C (Join-Path $Root "third_party\ComfyUI") pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "ComfyUI update failed" }
    & git -C (Join-Path $Root "third_party\Wan2GP") pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "Wan2GP update failed" }

    Invoke-Compose -CommandArgs @("pull","llm","reasoning")
    Set-BuildSourceHashes
    Invoke-Compose -CommandArgs @("build","docker-control","telemetry","supervisor","gateway","voice","eval-router","dashboard","mcp","agent-lab","agent-evaluator","agent-eval-runner","stt","vlm","comfyui","wangp")
    Invoke-Compose -CommandArgs @("--profile","gpu","create","--force-recreate","llm","reasoning","stt","tts","vlm","comfyui","wangp")
    Invoke-Compose -CommandArgs @("up","-d","--force-recreate","docker-control","telemetry","supervisor","gateway","voice","eval-router","dashboard","mcp","agent-lab-sandbox","agent-eval-runner","agent-evaluator","agent-lab")
    Wait-Gateway
    Wait-Dashboard
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
    if($state.ai_stack) {
      Assert-RootWorktreeClean
      $currentBranch = (& git -C $Root branch --show-current).Trim()
      $currentSha = (& git -C $Root rev-parse HEAD).Trim()
      Assert-SourceRollbackSafe -Snapshot $state.ai_stack -CurrentBranch $currentBranch -CurrentSha $currentSha
    }
    Assert-CleanTrackedRepo (Join-Path $Root "third_party\ComfyUI")
    Assert-CleanTrackedRepo (Join-Path $Root "third_party\Wan2GP")

    & git -C (Join-Path $Root "third_party\ComfyUI") reset --hard $state.comfyui.sha
    if ($LASTEXITCODE -ne 0) { throw "ComfyUI rollback failed" }
    & git -C (Join-Path $Root "third_party\Wan2GP") reset --hard $state.wangp.sha
    if ($LASTEXITCODE -ne 0) { throw "Wan2GP rollback failed" }

    if($state.ai_stack -and $currentSha -eq $state.ai_stack.updated_sha) {
      & git -C $Root reset --hard $state.ai_stack.previous_sha
      if ($LASTEXITCODE -ne 0) { throw "AI-Stack source rollback failed" }
    }

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
    Invoke-Compose -CommandArgs @("up","-d","--force-recreate","docker-control","telemetry","supervisor","gateway","voice","eval-router","dashboard","mcp","agent-lab-sandbox","agent-eval-runner","agent-evaluator","agent-lab")
    Wait-Gateway
    Wait-Dashboard
    Write-Output "Rollback complete from: $Snapshot"
  }
  "doctor" { & (Join-Path $PSScriptRoot "doctor.ps1") }
  "model-info" { & python (Join-Path $PSScriptRoot "gguf-info.py") "models\llm\daily.gguf" "models\llm\reasoning.gguf" }
  "smoke" { & (Join-Path $PSScriptRoot "smoke.ps1") }
  "voice-smoke" {
    Wait-Gateway
    & docker compose exec -T gateway python -m gateway.voice_smoke
    if ($LASTEXITCODE -ne 0) { throw "voice pipeline smoke test failed" }
  }
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
  "test-agent-lab" {
    Start-ControlPlane
    & docker run --rm ai-stack-agent-lab python -m unittest discover -s agent_lab/tests -v
    if ($LASTEXITCODE -ne 0) { throw "Agent Lab regression suite failed" }
  }
  "test-agent-evaluator" {
    Start-ControlPlane
    & docker run --rm ai-stack-agent-evaluator python -m unittest discover -s agent_eval/tests -v
    if ($LASTEXITCODE -ne 0) { throw "Agent evaluator regression suite failed" }
  }
  "bench-agent-lab" {
    Start-ControlPlane
    try {
      & docker exec ai-stack-agent-lab python -m agent_lab.benchmarks --fail-on-regression
      if ($LASTEXITCODE -ne 0) { throw "Agent Lab benchmark failed" }
    }
    finally {
      & $PSCommandPath stop-all
    }
  }
  "burn-in" {
    & (Join-Path $PSScriptRoot "burn-in.ps1")
    if ($LASTEXITCODE -ne 0) { throw "burn-in suite failed" }
  }
  "bench" { & (Join-Path $PSScriptRoot "benchmark-llm.ps1") -Service $Service }
  "logs" { & docker logs --tail 200 "ai-stack-$Service" }
  "down" { Invoke-Compose -CommandArgs @("--profile","gpu","down") }
}
