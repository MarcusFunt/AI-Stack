#Requires -Version 5.1
$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Log = Join-Path $Root "data\state\full-burnin-latest.log"
Set-Location $Root

$script:Results = [System.Collections.Generic.List[object]]::new()

function Run-Step {
  param(
    [Parameter(Mandatory=$true)][string]$Name,
    [Parameter(Mandatory=$true)][scriptblock]$Block
  )

  $started = Get-Date
  Write-Host ""
  Write-Host ("========== " + $Name + " ==========")
  try {
    & $Block
    $script:Results.Add([pscustomobject]@{
      Step = $Name
      Status = "PASS"
      Seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
      Error = ""
    })
    Write-Host ("[BURNIN PASS] " + $Name)
  }
  catch {
    $script:Results.Add([pscustomobject]@{
      Step = $Name
      Status = "FAIL"
      Seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
      Error = $_.Exception.Message
    })
    Write-Host ("[BURNIN FAIL] " + $Name + ": " + $_.Exception.Message)
  }
}
Remove-Item $Log -ErrorAction SilentlyContinue
Start-Transcript -Path $Log -Force | Out-Null
$exitCode = 0

try {
  Write-Host ("BURNIN START " + (Get-Date -Format o))
  Run-Step "doctor-pre" { & (Join-Path $PSScriptRoot "doctor.ps1") -Strict }
  Run-Step "proxy-ip-change" { & (Join-Path $PSScriptRoot "ai.ps1") test-proxy }
  Run-Step "lease-concurrency" { & (Join-Path $PSScriptRoot "ai.ps1") test-leases }
  Run-Step "end-to-end-smoke" { & (Join-Path $PSScriptRoot "ai.ps1") smoke }
  Run-Step "doctor-post" { & (Join-Path $PSScriptRoot "doctor.ps1") -Strict }

  Write-Host ""
  Write-Host "========== FINAL STATE =========="
  & docker compose --profile gpu ps -a
  Write-Host "--- supervisor status ---"
  & (Join-Path $PSScriptRoot "ai.ps1") status
  Write-Host "--- docker stats ---"
  & docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
  Write-Host "--- nvidia-smi ---"
  & nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw --format=csv,noheader

  Write-Host ""
  Write-Host "========== SUMMARY =========="
  $script:Results | Format-Table -AutoSize
  $failed = @($script:Results | Where-Object { $_.Status -eq "FAIL" })
  if($failed.Count -gt 0) {
    $exitCode = 1
    Write-Host ("BURNIN FAILED: " + $failed.Count + " step(s) failed")
  } else {
    Write-Host "ALL_BURNIN_STEPS_PASSED"
  }
  Write-Host ("BURNIN END " + (Get-Date -Format o))
}
finally {
  try { & (Join-Path $PSScriptRoot "ai.ps1") stop-all | Out-Host } catch {
    Write-Warning ("final GPU cleanup failed: " + $_.Exception.Message)
    $exitCode = 1
  }
  Stop-Transcript | Out-Null
}

exit $exitCode
