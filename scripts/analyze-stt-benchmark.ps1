#Requires -Version 5.1
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string[]]$Results,
  [string]$OutputDirectory = "",
  [ValidateRange(5000, 1000000)][int]$BootstrapSamples = 5000,
  [int]$Seed = 20260930
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BenchmarkRoot = Join-Path $Root "data\stt-benchmark"
if(-not $OutputDirectory) {
  $OutputDirectory = Join-Path $BenchmarkRoot ("results\combined-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
if(-not [System.IO.Path]::IsPathRooted($OutputDirectory)) {
  $OutputDirectory = Join-Path $Root $OutputDirectory
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$BenchmarkPrefix = [System.IO.Path]::GetFullPath($BenchmarkRoot).TrimEnd("\") + "\"
if(-not $OutputDirectory.StartsWith($BenchmarkPrefix, [StringComparison]::OrdinalIgnoreCase)) {
  throw "OutputDirectory must live under $BenchmarkRoot."
}
$ResolvedResults = @()
foreach($item in $Results) {
  $resolved = (Resolve-Path $item).Path
  if(-not $resolved.StartsWith($BenchmarkPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Every results file must live under $BenchmarkRoot."
  }
  $ResolvedResults += $resolved
}

Set-Location $Root
$PythonArgs = @(
  "-m", "stt.benchmark.analysis.report",
  "--results"
) + $ResolvedResults + @(
  "--output-dir", $OutputDirectory,
  "--bootstrap-samples", $BootstrapSamples,
  "--seed", $Seed
)
& python @PythonArgs
if($LASTEXITCODE -ne 0) {
  throw "STT benchmark report generation failed with exit code $LASTEXITCODE."
}
