#Requires -Version 5.1
[CmdletBinding()]
param(
  [string]$Manifest = "",
  [string]$Output = "",
  [string]$BenchmarkDataRoot = "",
  [string]$CodeDirectory = "",
  [string]$PythonExecutable = "python",
  [switch]$UseExistingStack,
  [switch]$InstallEddaRuntime,
  [string[]]$Models = @("edda","saga2","hviske"),
  [ValidateRange(1,16)][int]$BatchSize = 2,
  [ValidateRange(1,10)][int]$BeamSize = 5,
  [ValidateRange(0,2)][double]$CollarSeconds = 0.25,
  [string[]]$Revision = @(),
  [switch]$NoSpeakerAttributed
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BenchRoot = if($BenchmarkDataRoot) {
  [System.IO.Path]::GetFullPath($BenchmarkDataRoot)
} else {
  Join-Path $Root "data\stt-benchmark"
}
if(-not (Test-Path $BenchRoot)) { New-Item -ItemType Directory -Path $BenchRoot | Out-Null }
$BenchRoot = (Resolve-Path $BenchRoot).Path
function Assert-UnderBenchmarkRoot([string]$Path, [string]$Label) {
  $rootPrefix = $BenchRoot.TrimEnd("\") + "\"
  if(-not $Path.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "$Label must live under $BenchRoot."
  }
}

if(-not $Manifest) { $Manifest = Join-Path $BenchRoot "manifest.jsonl" }
if(-not (Test-Path $Manifest)) {
  throw "Benchmark manifest not found: $Manifest. See stt\benchmark\README.md."
}
$Manifest = (Resolve-Path $Manifest).Path
Assert-UnderBenchmarkRoot $Manifest "Manifest"
if(-not $Output) {
  $Output = Join-Path $BenchRoot ("results\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
New-Item -ItemType Directory -Force -Path $Output | Out-Null
$Output = (Resolve-Path $Output).Path
Assert-UnderBenchmarkRoot $Output "Output"
if($CodeDirectory) {
  $CodeDirectory = (Resolve-Path $CodeDirectory).Path
  Assert-UnderBenchmarkRoot $CodeDirectory "CodeDirectory"
  if(-not (Test-Path (Join-Path $CodeDirectory "benchmark\runner.py"))) {
    throw "CodeDirectory must contain benchmark\runner.py."
  }
}
if($PythonExecutable -notmatch '^[A-Za-z0-9_./-]+$') {
  throw "PythonExecutable must be a container executable path using letters, digits, '.', '_', '/', or '-'."
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
if(-not $UseExistingStack) {
  & (Join-Path $PSScriptRoot "ai.ps1") start gateway | Out-Null
  if($LASTEXITCODE -ne 0) { throw "failed to start AI-Stack control plane" }
}
& docker inspect ai-stack-stt 2>$null | Out-Null
if($LASTEXITCODE -ne 0) {
  throw "ai-stack-stt has not been created. Run .\scripts\ai.ps1 create first."
}

$leaseId = "stt-benchmark-" + [Guid]::NewGuid().ToString("N")
$acquired = $false
try {
  Invoke-Supervisor "POST" ("/acquire/stt?lease_id=" + $leaseId + "&profile=benchmark&exclusive=true") | Out-Null
  $acquired = $true
  if($InstallEddaRuntime) {
    if($Models -notcontains "edda") { throw "-InstallEddaRuntime requires edda in -Models." }
    & docker exec ai-stack-stt python -m venv --system-site-packages /edda-venv
    if($LASTEXITCODE -ne 0) { throw "failed to create the isolated Edda Python environment" }
    $sharedPackagesCode = 'import pathlib,site,sys; shared=f"/venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"; pathlib.Path(site.getsitepackages()[0],"ai_stack_shared.pth").write_text(shared + "\n",encoding="utf-8")'
    & docker exec ai-stack-stt /edda-venv/bin/python -c $sharedPackagesCode
    if($LASTEXITCODE -ne 0) { throw "failed to link shared base packages into the Edda environment" }
    & docker exec ai-stack-stt /edda-venv/bin/python -m pip install --no-cache-dir --disable-pip-version-check "transformers==5.10.1"
    if($LASTEXITCODE -ne 0) { throw "failed to install the pinned Edda Transformers runtime" }
    & docker exec ai-stack-stt /edda-venv/bin/python -c "import torch,transformers; print('Edda runtime:', transformers.__version__, 'Torch:', torch.__version__)"
    if($LASTEXITCODE -ne 0) { throw "the isolated Edda runtime could not import its model dependencies" }
  }
  & docker exec ai-stack-stt python -c "import urllib.request; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/internal/benchmark/unload',method='POST'),timeout=30).read()"
  if($LASTEXITCODE -ne 0) { throw "failed to unload the resident faster-whisper model" }
  $containerCodeDirectory = if($CodeDirectory) { To-ContainerPath $CodeDirectory } else { $null }
  $args = @("exec")
  if($containerCodeDirectory) {
    $args += @("-w",$containerCodeDirectory,"-e",("PYTHONPATH=" + $containerCodeDirectory))
  }
  $args += @(
    "ai-stack-stt",$PythonExecutable,"-m","benchmark.runner",
    "--manifest",(To-ContainerPath $Manifest),
    "--output",(To-ContainerPath $Output),
    "--batch-size",$BatchSize,
    "--beam-size",$BeamSize,
    "--collar",$CollarSeconds,
    "--models"
  ) + $Models
  foreach($item in $Revision) {
    if($item -notmatch '^[A-Za-z0-9_-]+=[^=]+$') {
      throw "Invalid -Revision '$item'. Expected alias=revision."
    }
    $args += @("--revision", $item)
  }
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
