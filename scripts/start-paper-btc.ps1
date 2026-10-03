param(
    [ValidateRange(1, 100)][int]$Markets = 1,
    [switch]$Review,
    [ValidateRange(0, 3600)][int]$ResolutionWaitSeconds = 300,
    [ValidateRange(50, 4096)][int]$MaxNewLogMB = 1024
)

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Create the paper-only environment first: py -3 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e .'
}
Push-Location $project
try {
    if ($Review) {
        & $python -m polymarket_bot.paper review --runs runs
    } else {
        & $python -m polymarket_bot.paper run --runs runs --markets $Markets --resolution-wait $ResolutionWaitSeconds --max-new-log-mb $MaxNewLogMB
    }
    if ($LASTEXITCODE -ne 0) { throw "Paper simulator exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}

