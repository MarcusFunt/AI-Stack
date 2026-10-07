#Requires -Version 5.1
param(
  [switch] $VisualMemoryOnly
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$pythonArgs = @((Join-Path $PSScriptRoot "smoke.py"))
if($VisualMemoryOnly) {
  $pythonArgs += "--visual-memory-only"
}
& python @pythonArgs
if ($LASTEXITCODE -ne 0) {
  throw "end-to-end smoke suite failed"
}
