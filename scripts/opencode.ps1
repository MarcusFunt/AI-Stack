#Requires -Version 5.1
[CmdletBinding()]
param(
  [Parameter(Position=0)]
  [ValidateSet("tui","mini","run","models","mcp","api","serve","stop-server","pair","doctor")]
  [string]$Action = "tui",
  [Parameter(Position=1, ValueFromRemainingArguments=$true)]
  [string[]]$OpenCodeArgs
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$EnvPath = Join-Path $Root ".env"
$ServerUrl = "http://127.0.0.1:4096"
$ServerOut = Join-Path $Root "data\state\opencode-server.log"
$ServerErr = Join-Path $Root "data\state\opencode-server-error.log"
Set-Location $Root

function Import-AIStackEnv {
  if (-not (Test-Path $EnvPath)) { throw ".env is missing" }
  $wanted = @("AI_API_KEY","MCP_API_KEY","OPENCODE_SERVER_PASSWORD")
  foreach ($line in Get-Content $EnvPath) {
    if (-not $line -or $line.TrimStart().StartsWith("#") -or $line -notmatch "=") { continue }
    $parts = $line -split "=", 2
    $name = $parts[0].Trim()
    if ($wanted -contains $name) {
      [Environment]::SetEnvironmentVariable($name, $parts[1], "Process")
    }
  }
}

function Require-OpenCode {
  $cmd = Get-Command opencode.cmd -ErrorAction SilentlyContinue
  if (-not $cmd) { $cmd = Get-Command opencode -ErrorAction SilentlyContinue }
  if (-not $cmd) { throw "OpenCode is not installed. Run: npm install -g @opencode/cli" }
  return $cmd.Source
}

function Get-OpenCodeAuthHeader {
  $raw = "opencode:$env:OPENCODE_SERVER_PASSWORD"
  $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($raw))
  return @{ Authorization = "Basic $encoded" }
}

function Test-OpenCodeServer {
  try {
    $null = Invoke-RestMethod ($ServerUrl + "/api/info") -Headers (Get-OpenCodeAuthHeader) -TimeoutSec 2
    return $true
  } catch {
    return $false
  }
}

function Start-AIStackControlPlane {
  & (Join-Path $PSScriptRoot "ai.ps1") start gateway | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "AI-Stack control plane failed to start" }
}

function Start-OpenCodeServer {
  if (Test-OpenCodeServer) { return }
  $listener = Get-NetTCPConnection -LocalPort 4096 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($listener) {
    $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    if (-not $process -or $process.ProcessName -ne "opencode") {
      throw "Port 4096 is already owned by another process"
    }
    Stop-Process -Id $listener.OwningProcess -Force
    Start-Sleep -Milliseconds 300
  }

  New-Item -ItemType Directory -Force (Split-Path $ServerOut) | Out-Null
  $serveArgs = @("serve","--hostname","127.0.0.1","--port","4096","--cors","http://127.0.0.1:3000","--cors","http://localhost:3000")
  Start-Process -FilePath $OpenCode -ArgumentList $serveArgs -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $ServerOut -RedirectStandardError $ServerErr | Out-Null

  for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 250
    if (Test-OpenCodeServer) { return }
  }
  $tail = if (Test-Path $ServerErr) { (Get-Content $ServerErr -Tail 30) -join [Environment]::NewLine } else { "" }
  throw "OpenCode server did not become ready. $tail"
}

function Stop-OpenCodeServer {
  $listener = Get-NetTCPConnection -LocalPort 4096 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $listener) { return }
  $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
  if ($process -and $process.ProcessName -eq "opencode") {
    Stop-Process -Id $listener.OwningProcess -Force
  } else {
    throw "Refusing to stop non-OpenCode process on port 4096"
  }
}

Import-AIStackEnv
$OpenCode = Require-OpenCode
foreach ($required in @("AI_API_KEY","MCP_API_KEY","OPENCODE_SERVER_PASSWORD")) {
  if (-not [Environment]::GetEnvironmentVariable($required, "Process")) {
    throw "$required is missing from .env"
  }
}

switch ($Action) {
  "doctor" {
    Write-Host ("OpenCode: " + (& $OpenCode --version))
    if (-not (Test-Path (Join-Path $Root "opencode.jsonc"))) { throw "opencode.jsonc is missing" }
    Get-Content (Join-Path $Root "opencode.jsonc") -Raw | ConvertFrom-Json | Out-Null
    Start-AIStackControlPlane
    Start-OpenCodeServer
    $headers = @{ Authorization = "Bearer $env:AI_API_KEY" }
    $health = Invoke-RestMethod "http://127.0.0.1:8090/health" -Headers $headers -TimeoutSec 5
    if ($health.status -ne "ok") { throw "Gateway health check failed" }
    $mcp = Invoke-RestMethod "http://127.0.0.1:8765/healthz" -TimeoutSec 5
    if ($mcp.status -ne "ok") { throw "MCP health check failed" }
    if (-not (Test-OpenCodeServer)) { throw "OpenCode server health check failed" }
    Write-Host "OpenCode, AI gateway, and MCP bridge are healthy."
  }
  "models" {
    $config = Get-Content (Join-Path $Root "opencode.jsonc") -Raw | ConvertFrom-Json
    foreach ($provider in $config.providers.PSObject.Properties) {
      foreach ($model in $provider.Value.models.PSObject.Properties) {
        Write-Output ($provider.Name + "/" + $model.Name + " - " + $model.Value.name)
      }
    }
  }
  "mcp" {
    Start-AIStackControlPlane
    Start-OpenCodeServer
    $response = Invoke-RestMethod -Method Get -Uri ($ServerUrl + "/api/mcp") -Headers (Get-OpenCodeAuthHeader) -TimeoutSec 10
    $items = if ($response.data) { @($response.data) } else { @($response) }
    foreach ($item in $items) {
      $state = if ($item.status.status) { $item.status.status } else { "unknown" }
      Write-Output ($item.name + " - " + $state)
    }
  }
  "api" {
    Start-AIStackControlPlane
    Start-OpenCodeServer
    & $OpenCode api --server $ServerUrl @OpenCodeArgs
  }
  "run" {
    Start-AIStackControlPlane
    Start-OpenCodeServer
    if (-not $OpenCodeArgs -or $OpenCodeArgs.Count -eq 0) {
      throw "Usage: .\scripts\opencode.ps1 run <prompt>"
    }
    $prompt = ($OpenCodeArgs -join " ")
    $sessionBody = @{ title = "AI-Stack scripted run" } | ConvertTo-Json -Compress
    $session = Invoke-RestMethod -Method Post -Uri ($ServerUrl + "/api/session") -Headers (Get-OpenCodeAuthHeader) -ContentType "application/json" -Body $sessionBody -TimeoutSec 10
    $sessionData = if ($session.data) { $session.data } else { $session }
    $sessionID = $sessionData.id
    if (-not $sessionID) { throw "OpenCode did not return a session ID" }
    try {
      $payload = @{ agent = "build"; text = $prompt } | ConvertTo-Json -Depth 8
      Invoke-RestMethod -Method Post -Uri ($ServerUrl + "/api/session/$sessionID/prompt") -Headers (Get-OpenCodeAuthHeader) -ContentType "application/json" -Body $payload -TimeoutSec 30 | Out-Null
      $deadline = [DateTime]::UtcNow.AddMinutes(10)
      $messages = $null
      do {
        Start-Sleep -Milliseconds 500
        $response = Invoke-RestMethod -Method Get -Uri ($ServerUrl + "/api/session/$sessionID/message") -Headers (Get-OpenCodeAuthHeader) -TimeoutSec 10
        $messages = if ($response.data) { @($response.data) } else { @($response) }
        $idle = $messages | Where-Object { $_.type -eq "idle" } | Select-Object -First 1
        if ($idle) { break }
      } while ([DateTime]::UtcNow -lt $deadline)
      if (-not $idle) { throw "OpenCode scripted run timed out waiting for session completion" }
      if ($idle.outcome -and $idle.outcome -ne "succeeded") { throw "OpenCode session ended with outcome: $($idle.outcome)" }
      $assistant = $messages | Where-Object { $_.type -eq "assistant" } | Select-Object -First 1
      if (-not $assistant) { throw "OpenCode completed without an assistant message" }
      $texts = @($assistant.content | Where-Object { $_.type -eq "text" } | ForEach-Object { $_.text })
      if ($texts.Count -gt 0) { $texts | ForEach-Object { Write-Output $_ } }
      else { $assistant | ConvertTo-Json -Depth 20 }
    }
    finally {
      try { Invoke-RestMethod -Method Delete -Uri ($ServerUrl + "/api/session/$sessionID") -Headers (Get-OpenCodeAuthHeader) -TimeoutSec 10 | Out-Null } catch {}
    }
  }
  "mini" {
    Start-AIStackControlPlane
    Start-OpenCodeServer
    & $OpenCode mini --server $ServerUrl @OpenCodeArgs
  }
  "serve" {
    Start-AIStackControlPlane
    Start-OpenCodeServer
    Write-Host "OpenCode server ready at $ServerUrl"
  }
  "stop-server" {
    Stop-OpenCodeServer
    Write-Host "OpenCode server stopped."
  }
  "pair" {
    Start-AIStackControlPlane
    Start-OpenCodeServer
    Write-Host "URL: $ServerUrl"
    Write-Host "Username: opencode"
    Write-Host "Password: stored in D:\AI-Stack\.env as OPENCODE_SERVER_PASSWORD"
  }
  default {
    Start-AIStackControlPlane
    Start-OpenCodeServer
    & $OpenCode --server $ServerUrl @OpenCodeArgs
  }
}
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
