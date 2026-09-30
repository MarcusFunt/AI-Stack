#Requires -Version 5.1
[CmdletBinding()]
param(
  [ValidateSet("samtalebank-sam3", "diarization-k3", "coral-conversation-test", "nst-da-test", "fleurs-da-dk-test", "danpass-dialogue")][string]$Suite = "samtalebank-sam3",
  [string]$SourcePath = "",
  [string]$OutputDirectory = "",
  [string]$BenchmarkDataRoot = "",
  [string]$LockPath = "",
  [int]$Seed = 20260930,
  [string]$Revision = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BenchmarkRoot = if($BenchmarkDataRoot) {
  [System.IO.Path]::GetFullPath($BenchmarkDataRoot)
} else {
  Join-Path $Root "data\stt-benchmark"
}
if(-not (Test-Path $BenchmarkRoot)) {
  New-Item -ItemType Directory -Force -Path $BenchmarkRoot | Out-Null
}
if(-not $OutputDirectory) {
  $OutputDirectory = Join-Path $BenchmarkRoot ("prepared\" + $Suite)
}
if(-not [System.IO.Path]::IsPathRooted($OutputDirectory)) {
  $OutputDirectory = Join-Path $BenchmarkRoot $OutputDirectory
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$RootPrefix = [System.IO.Path]::GetFullPath($BenchmarkRoot).TrimEnd("\") + "\"
if(-not $OutputDirectory.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "OutputDirectory must live under $BenchmarkRoot."
}
if(-not $LockPath) { $LockPath = Join-Path $BenchmarkRoot "dataset-lock.json" }
if(-not [System.IO.Path]::IsPathRooted($LockPath)) {
  $LockPath = Join-Path $BenchmarkRoot $LockPath
}
$LockPath = [System.IO.Path]::GetFullPath($LockPath)
if(-not $LockPath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "LockPath must live under $BenchmarkRoot."
}
$PythonArgs = @(
  "-m", "stt.benchmark.datasets.prepare",
  "--suite", $Suite,
  "--output-dir", $OutputDirectory,
  "--lock-path", $LockPath,
  "--seed", $Seed
)
if($Revision) {
  $PythonArgs += @("--revision", $Revision)
}
if($SourcePath) {
  $PythonArgs += @("--source-path", (Resolve-Path $SourcePath).Path)
}
Set-Location $Root
& python @PythonArgs
if($LASTEXITCODE -ne 0) {
  throw "STT dataset preparation failed with exit code $LASTEXITCODE."
}
