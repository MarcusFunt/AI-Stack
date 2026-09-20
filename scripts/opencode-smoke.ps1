#Requires -Version 5.1
[CmdletBinding()]
param(
  [string]$Prompt = "Reply with exactly OPENCODE_OK and nothing else."
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$EnvPath = Join-Path $Root ".env"
$ServerUrl = "http://127.0.0.1:4096"

& (Join-Path $PSScriptRoot "opencode.ps1") serve | Out-Host

$line = Get-Content $EnvPath | Where-Object { $_ -match '^OPENCODE_SERVER_PASSWORD=' } | Select-Object -Last 1
if (-not $line) { throw "OPENCODE_SERVER_PASSWORD is missing" }
$password = ($line -split "=", 2)[1]
$raw = "opencode:$password"
$basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($raw))
$headers = @{ Authorization = "Basic $basic" }

$session = Invoke-RestMethod -Method Post -Uri ($ServerUrl + "/api/session") -Headers $headers -ContentType "application/json" -Body '{"title":"AI-Stack OpenCode smoke"}' -TimeoutSec 10
$sessionData = if ($session.data) { $session.data } else { $session }
$sessionID = $sessionData.id
if (-not $sessionID) { throw "OpenCode did not return a session ID" }

try {
  $payload = @{
    agent = "build"
    text = $Prompt
  } | ConvertTo-Json -Depth 8
  Invoke-RestMethod -Method Post -Uri ($ServerUrl + "/api/session/$sessionID/prompt") -Headers $headers -ContentType "application/json" -Body $payload -TimeoutSec 30 | Out-Null
  $deadline = [DateTime]::UtcNow.AddMinutes(5)
  do {
    Start-Sleep -Milliseconds 500
    $response = Invoke-RestMethod -Method Get -Uri ($ServerUrl + "/api/session/$sessionID/message") -Headers $headers -TimeoutSec 10
    $messages = if ($response.data) { @($response.data) } else { @($response) }
    $idle = $messages | Where-Object { $_.type -eq "idle" } | Select-Object -First 1
    if ($idle) { break }
  } while ([DateTime]::UtcNow -lt $deadline)
  if (-not $idle) { throw "OpenCode smoke test timed out" }
  if ($idle.outcome -ne "succeeded") { throw "OpenCode smoke session outcome: $($idle.outcome)" }
  $assistant = $messages | Where-Object { $_.type -eq "assistant" } | Select-Object -First 1
  $text = (@($assistant.content | Where-Object { $_.type -eq "text" } | ForEach-Object { $_.text }) -join [Environment]::NewLine).Trim()
  if ($text -ne "OPENCODE_OK") {
    throw "OpenCode completed but returned unexpected text: $text"
  }
  Write-Host "OpenCode end-to-end smoke test passed: OPENCODE_OK"
}
finally {
  try {
    Invoke-RestMethod -Method Delete -Uri ($ServerUrl + "/api/session/$sessionID") -Headers $headers -TimeoutSec 10 | Out-Null
  } catch {}
}
