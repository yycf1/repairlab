param(
    [ValidateRange(1, 65535)][int]$Port = 8766,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    $pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw 'uv is not installed. Install uv, then retry this launcher.'
        }
        & uv sync --extra dev
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
    }
    $launchArguments = @((Join-Path $PSScriptRoot 'launch_workbench.py'), '--port', "$Port")
    if ($NoBrowser) { $launchArguments += '--no-browser' }
    & $pythonPath @launchArguments
    if ($LASTEXITCODE -ne 0) { throw 'Workbench startup failed. Read the messages above.' }
} finally {
    Pop-Location
}
