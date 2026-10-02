#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$script:PassCount = 0
$script:Failures = 0
$script:DockerSequence = @()
$script:DockerCalls = 0
$script:DockerDesktopExists = $false
$script:HideDockerCommand = $false
$script:TailscaleCalls = @()
$script:TailscaleState = "Running"
$script:TailscaleServiceStatus = "Stopped"
$script:TailscaleServiceStarts = 0
$script:HideTailscaleCommand = $false
$script:ActiveJobsJson = '{"llm":0,"tts":0,"stt":0}'

function Assert-True([bool]$Condition, [string]$Message) {
  if (-not $Condition) { throw $Message }
  $script:PassCount++
  Write-Output ("[PASS] " + $Message)
}

function Assert-Fails([scriptblock]$Action, [string]$Pattern, [string]$Message) {
  $observed = $false
  try { & $Action | Out-Null }
  catch { $observed = $_.Exception.Message -match $Pattern }
  Assert-True $observed $Message
}

function Get-Command {
  [CmdletBinding()]
  param([Parameter(Position=0)][string]$Name)
  if ($script:HideDockerCommand -and $Name -eq "docker") { return $null }
  if ($script:HideTailscaleCommand -and $Name -eq "tailscale.exe") { return $null }
  Microsoft.PowerShell.Core\Get-Command -Name $Name -ErrorAction SilentlyContinue
}

function Test-Path {
  param([string]$Path, [string]$LiteralPath)
  if (-not $Path) { $Path = $LiteralPath }
  if ($Path -like "*Docker Desktop.exe") { return $script:DockerDesktopExists }
  if ($Path -like "*Tailscale\tailscale.exe") { return $false }
  Microsoft.PowerShell.Management\Test-Path -LiteralPath $Path
}

function Start-Process {
  param([string]$FilePath, [string[]]$ArgumentList, [string]$WindowStyle, [string]$WorkingDirectory, [string]$RedirectStandardOutput, [string]$RedirectStandardError)
  $script:StartedProcesses += @($FilePath)
}

function Start-Sleep {
  param([int]$Seconds, [int]$Milliseconds)
  $script:SleepCalls++
}

function Start-Service {
  [CmdletBinding()]
  param([string]$Name)
  $script:TailscaleServiceStarts++
}

function Get-Service {
  [CmdletBinding()]
  param([string]$Name)
  [pscustomobject]@{ Status = $script:TailscaleServiceStatus }
}

function docker {
  $script:DockerCalls++
  $index = [math]::Min($script:DockerCalls - 1, $script:DockerSequence.Count - 1)
  if ($index -lt 0) { $global:LASTEXITCODE = 1 }
  else { $global:LASTEXITCODE = [int]$script:DockerSequence[$index] }
}

function tailscale.exe {
  $script:TailscaleCalls += ,@($args)
  $global:LASTEXITCODE = 0
  if ($script:TailscaleState -eq "NeedsLogin") {
    '{"BackendState":"NeedsLogin","Self":{"Online":false}}'
    $script:TailscaleState = "Running"
  } else {
    '{"BackendState":"Running","Self":{"Online":true}}'
  }
}

function Run-Git([string]$Path, [string[]]$GitArgs) {
  $previousPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = & git -C $Path @GitArgs 2>&1
    $exitCode = $LASTEXITCODE
  } finally { $ErrorActionPreference = $previousPreference }
  if ($exitCode -ne 0) { throw ("git " + ($GitArgs -join " ") + " failed: " + ($output -join " ")) }
  return $output
}

function Invoke-Supervisor([string]$Method, [string]$Path, [int]$Timeout=600) {
  $script:SupervisorCalls++
  return '{"active_jobs":' + $script:ActiveJobsJson + '}'
}

function Write-Commit([string]$Path, [string]$Name) {
  Set-Content -LiteralPath (Join-Path $Path $Name) -Value ([guid]::NewGuid().ToString("N")) -Encoding ASCII
  Run-Git $Path @("add", $Name) | Out-Null
  Run-Git $Path @("commit", "-m", $Name) | Out-Null
}

$scriptPath = (Resolve-Path (Join-Path $PSScriptRoot "..\..\scripts\ai.ps1")).Path
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
$scriptText = [IO.File]::ReadAllText($scriptPath)
$voiceSmokeAction = [regex]::Match($scriptText, '(?ms)^\s*"voice-smoke"\s*\{(.*?)^\s*\}')
if (-not $voiceSmokeAction.Success) { throw "voice-smoke CLI action was not found" }
Assert-True ($voiceSmokeAction.Groups[1].Value -match 'Wait-Gateway' -and $voiceSmokeAction.Groups[1].Value -match 'gateway\.voice_smoke' -and $voiceSmokeAction.Groups[1].Value -notmatch 'Start-ControlPlane|stop-all') "voice smoke CLI reuses the running gateway without restarting services or stopping GPU workers"
$functionsToLoad = @("Test-DockerEngine", "Ensure-DockerEngine", "Start-TailscaleBestEffort", "Get-RootSourceUpdate", "Assert-RootWorktreeClean", "Assert-CurrentSupervisorIdle", "New-SourceRevisionSnapshot", "Assert-SourceRollbackSafe")
$functionAsts = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $functionsToLoad -contains $node.Name }, $true)
foreach ($functionAst in $functionAsts) { . ([scriptblock]::Create($functionAst.Extent.Text)) }

$tempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$tempRoot = Join-Path $tempParent ("ai-stack-launch-tests-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempRoot | Out-Null
try {
  $script:StartedProcesses = @()
  $script:SleepCalls = 0
  $script:DockerSequence = @(0)
  $script:DockerCalls = 0
  $script:DockerDesktopExists = $false
  $script:HideDockerCommand = $false
  Ensure-DockerEngine | Out-Null
  Assert-True ($script:DockerCalls -eq 1 -and $script:StartedProcesses.Count -eq 0) "Docker already running skips Desktop launch"

  $script:DockerSequence = @(1, 0)
  $script:DockerCalls = 0
  $script:DockerDesktopExists = $true
  $script:StartedProcesses = @()
  Ensure-DockerEngine | Out-Null
  Assert-True ($script:DockerCalls -eq 2 -and $script:StartedProcesses.Count -eq 1) "stopped Docker starts Desktop and waits for Engine"

  $script:DockerSequence = @(1)
  $script:DockerCalls = 0
  $script:DockerDesktopExists = $true
  $script:StartedProcesses = @()
  $script:SleepCalls = 0
  Assert-Fails { Ensure-DockerEngine } "within 3 minutes" "Docker Engine timeout produces a clear failure"
  Assert-True ($script:StartedProcesses.Count -eq 1 -and $script:SleepCalls -eq 90) "Docker timeout is bounded and only launches Desktop once"

  $script:HideDockerCommand = $true
  Assert-Fails { Ensure-DockerEngine } "Docker CLI was not found" "missing Docker CLI is reported without launching anything"
  $script:HideDockerCommand = $false

  $script:TailscaleCalls = @()
  $script:TailscaleState = "NeedsLogin"
  $script:TailscaleServiceStatus = "Stopped"
  $script:TailscaleServiceStarts = 0
  Start-TailscaleBestEffort | Out-Null
  Assert-True ($script:TailscaleServiceStarts -eq 1 -and $script:TailscaleCalls.Count -eq 2) "offline Tailscale starts its service and rechecks status"
  $routeCommands = @($script:TailscaleCalls | ForEach-Object { $_[0] })
  Assert-True (($routeCommands -join ",") -eq "status,status") "Tailscale startup only queries status and leaves Serve/Funnel routes untouched"

  $script:SupervisorCalls = 0
  $script:ActiveJobsJson = '{"llm":1}'
  Assert-Fails { Assert-CurrentSupervisorIdle } "active AI jobs prevent maintenance" "voice smoke refuses active GPU jobs before control-plane startup"
  Assert-True ($script:SupervisorCalls -eq 1) "GPU preflight checks existing supervisor once without starting services"
  $script:ActiveJobsJson = '{"llm":0,"tts":0,"stt":0}'

  $bare = Join-Path $tempRoot "origin.git"
  $root = Join-Path $tempRoot "work"
  $writer = Join-Path $tempRoot "writer"
  New-Item -ItemType Directory -Path $bare | Out-Null
  Run-Git $bare @("init", "--bare", "--initial-branch=main") | Out-Null
  Run-Git $tempRoot @("clone", $bare, $root) | Out-Null
  Run-Git $root @("config", "user.name", "AI Stack Tests") | Out-Null
  Run-Git $root @("config", "user.email", "ai-stack-tests@example.invalid") | Out-Null
  Write-Commit $root "base.txt"
  Run-Git $root @("push", "--set-upstream", "origin", "main") | Out-Null
  Run-Git $tempRoot @("clone", $bare, $writer) | Out-Null
  Run-Git $writer @("config", "user.name", "AI Stack Tests") | Out-Null
  Run-Git $writer @("config", "user.email", "ai-stack-tests@example.invalid") | Out-Null

  $script:Root = $root
  Assert-RootWorktreeClean
  Write-Commit $writer "remote-next.txt"
  Run-Git $writer @("push", "origin", "main") | Out-Null
  $update = Get-RootSourceUpdate
  Assert-True ($update.branch -eq "main" -and $update.previous_sha -ne $update.updated_sha) "origin/main update records current branch and fast-forward target"
  $snapshot = New-SourceRevisionSnapshot -SourceUpdate $update
  Assert-True ($snapshot.previous_sha -eq $update.previous_sha -and $snapshot.updated_sha -eq $update.updated_sha -and $snapshot.branch -eq $update.branch) "rollback snapshot records both source revisions and the current branch"
  Assert-SourceRollbackSafe -Snapshot $snapshot -CurrentBranch $update.branch -CurrentSha $update.previous_sha
  Assert-SourceRollbackSafe -Snapshot $snapshot -CurrentBranch $update.branch -CurrentSha $update.updated_sha
  Assert-Fails { Assert-SourceRollbackSafe -Snapshot $snapshot -CurrentBranch "other-branch" -CurrentSha $update.updated_sha } "refusing guarded source rollback" "source rollback refuses a branch change"
  Assert-Fails { Assert-SourceRollbackSafe -Snapshot $snapshot -CurrentBranch $update.branch -CurrentSha ("f" * 40) } "refusing guarded source rollback" "source rollback refuses an unknown current revision"
  $beforeMerge = (Run-Git $root @("rev-parse", "HEAD")).Trim()
  Run-Git $root @("merge", "--ff-only", "origin/main") | Out-Null
  $afterMerge = (Run-Git $root @("rev-parse", "HEAD")).Trim()
  $afterBranch = (Run-Git $root @("branch", "--show-current")).Trim()
  Assert-True ($beforeMerge -eq $update.previous_sha -and $afterMerge -eq $update.updated_sha -and $afterBranch -eq "main") "fast-forward advances the current branch without switching"

  Write-Commit $root "local-only.txt"
  Write-Commit $writer "remote-diverged.txt"
  Run-Git $writer @("push", "origin", "main") | Out-Null
  $localBeforeRefusal = (Run-Git $root @("rev-parse", "HEAD")).Trim()
  Assert-Fails { Get-RootSourceUpdate } "cannot fast-forward" "diverged branch is refused"
  $localAfterRefusal = (Run-Git $root @("rev-parse", "HEAD")).Trim()
  Assert-True ($localBeforeRefusal -eq $localAfterRefusal) "diverged update leaves the current source revision unchanged"

  New-Item -ItemType File -Path (Join-Path $root "untracked-test-file.txt") | Out-Null
  Assert-Fails { Assert-RootWorktreeClean } "tracked or untracked changes" "dirty worktree is refused"

  Write-Output ("Passed " + $script:PassCount + " launcher and update checks.")
} finally {
  $resolvedTempRoot = [IO.Path]::GetFullPath((Resolve-Path $tempRoot).Path)
  if (-not $resolvedTempRoot.StartsWith($tempParent, [StringComparison]::OrdinalIgnoreCase)) {
    throw "refusing to clean test directory outside the system temp folder"
  }
  Remove-Item -LiteralPath $resolvedTempRoot -Recurse -Force
}
