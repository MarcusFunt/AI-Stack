#Requires -Version 5.1
[CmdletBinding()]
param(
  [ValidateSet("local-fast","local-reasoning","both")]
  [string]$Model = "local-fast",
  [double]$MinPassRate = 1.0,
  [switch]$FullAgentCoding
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
$labVolume = (Join-Path $Root "agent_lab") + ":/app/agent_lab:ro"
$Failures = @()

$models = if ($Model -eq "both") {
  @("local-fast", "local-reasoning")
} else {
  @($Model)
}

foreach ($name in $models) {
  Write-Output ""
  Write-Output ("=== Raw model quality: " + $name + " ===")
  docker compose run --rm --no-deps -v $labVolume agent-lab python -m agent_lab.model_quality --model $name --min-pass-rate $MinPassRate --fail-on-regression
  if ($LASTEXITCODE -ne 0) { $Failures += "Raw model quality failed for $name" }
  Write-Output ""
  Write-Output ("=== Agent coding quality: " + $name + " ===")
  $args = @("-m","agent_lab.benchmarks","--model",$name,"--fail-on-regression")
  if (-not $FullAgentCoding) {
    $args += @(
      "--case","add-operator",
      "--case","create-backoff-module",
      "--case","create-and-repair",
      "--case","syntax-colon",
      "--case","bounded-history",
      "--case","parse-timeout",
      "--case","merge-defaults"
    )
  }
  docker compose run --rm --no-deps -v $labVolume agent-lab python @args
  if ($LASTEXITCODE -ne 0) { $Failures += "Agent coding quality failed for $name" }
}

Write-Output ""
if ($Failures.Count -gt 0) {
  $Failures | ForEach-Object { Write-Output ("[FAIL] " + $_) }
  throw ($Failures.Count.ToString() + " AI test stage(s) failed")
}
Write-Output "ALL_AI_TESTS_PASSED"
