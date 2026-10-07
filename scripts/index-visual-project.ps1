param(
    [Parameter(Mandatory = $true)]
    [string] $ProjectRoot,

    [Parameter(Mandatory = $true)]
    [string] $Namespace,

    [string] $GatewayUrl = "http://127.0.0.1:8090",
    [ValidateRange(1, 32000)]
    [int] $ChunkChars = 8000,
    [ValidateRange(1, 10485760)]
    [int] $MaxFileBytes = 1048576
)

$scriptPath = Join-Path $PSScriptRoot "index_visual_project.py"
& python $scriptPath --project-root $ProjectRoot --namespace $Namespace --gateway-url $GatewayUrl --chunk-chars $ChunkChars --max-file-bytes $MaxFileBytes
exit $LASTEXITCODE
