#Requires -Version 5.1
[CmdletBinding()]
param(
  [string]$Manifest = "",
  [string]$Output = "",
  [string[]]$Models = @("edda","saga2","hviske"),
  [ValidateRange(1,16)][int]$BatchSize = 2,
  [ValidateRange(1,10)][int]$BeamSize = 5,
  [ValidateRange(0,2)][double]$CollarSeconds = 0.25,
  [switch]$NoSpeakerAttributed
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BenchRoot = Join-Path $Root "data\stt-benchmark"
if(-not (Test-Path $BenchRoot)) { New-Item -ItemType Directory -Path $BenchRoot | Out-Null }
if(-not $Manifest) { $Manifest = Join-Path $BenchRoot "manifest.jsonl" }
if(-not (Test-Path $Manifest)) {
  throw "Benchmark manifest not found: $Manifest. See stt\benchmark\README.md."
}
$Manifest = (Resolve-Path $Manifest).Path
if(-not $Manifest.StartsWith($BenchRoot, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Manifest must live under $BenchRoot so the STT container can access its audio."
}
if(-not $Output) {
  $Output = Join-Path $BenchRoot ("results\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$Output = (Resolve-Path $Output).Path
if(-not $Output.StartsWith($BenchRoot, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Output must live under $BenchRoot."
}

function To-ContainerPath([string]$Path) {
  $relative = $Path.Substring($BenchRoot.Length).TrimStart("\")
  return "/benchmark/" + ($relative -replace "\\","/")
}
function Invoke-Supervisor([string]$Method, [string]$Path, [int]$Timeout = 600) {
  $code = "import os,urllib.request; h={'X-Supervisor-Token':os.environ['SUPERVISOR_TOKEN']}; r=urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000$Path',headers=h,method='$Method'),timeout=$Timeout); print(r.read().decode())"
  & docker exec ai-stack-supervisor python -c $code
  if($LASTEXITCODE -ne 0) { throw "supervisor request failed: $Method $Path" }
}

Set-Location $Root
& (Join-Path $PSScriptRoot "ai.ps1") start gateway | Out-Null
if($LASTEXITCODE -ne 0) { throw "failed to start AI-Stack control plane" }

$leaseId = "stt-benchmark-" + [Guid]::NewGuid().ToString("N")
$acquired = $false
try {
  Invoke-Supervisor "POST" ("/acquire/stt?lease_id=" + $leaseId) | Out-Null
  $acquired = $true
  $args = @(
    "exec","ai-stack-stt","python","-m","benchmark.runner",
    "--manifest",(To-ContainerPath $Manifest),
    "--output",(To-ContainerPath $Output),
    "--batch-size",$BatchSize,
    "--beam-size",$BeamSize,
    "--collar",$CollarSeconds,
    "--models"
  ) + $Models
  if($NoSpeakerAttributed) { $args += "--no-speaker-attributed" }
  & docker @args
  if($LASTEXITCODE -ne 0) { throw "STT benchmark failed with exit code $LASTEXITCODE" }
} finally {
  if($acquired) {
    try { Invoke-Supervisor "POST" ("/release/stt?lease_id=" + $leaseId) 60 | Out-Null }
    catch { Write-Warning ("Failed to release STT benchmark lease: " + $_) }
  }
}
Write-Output ("Benchmark results: " + $Output)
