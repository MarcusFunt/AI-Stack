#Requires -Version 5.1
[CmdletBinding()]
param(
  [double]$DurationHours = 8.0,
  [string]$Catalog = ""
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if(-not $Catalog) {
  $Catalog = Join-Path $Root "config\self-improve-features.json"
}
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if(-not (Test-Path $Python)) {
  throw "project virtualenv Python not found: $Python"
}

& (Join-Path $PSScriptRoot "ai.ps1") start gateway
try {
  & $Python (Join-Path $PSScriptRoot "self_improve.py") --catalog $Catalog --duration-hours $DurationHours
  if($LASTEXITCODE -ne 0) {
    throw "self-improvement campaign failed with exit code $LASTEXITCODE"
  }
}
finally {
  & (Join-Path $PSScriptRoot "ai.ps1") stop-all
}
