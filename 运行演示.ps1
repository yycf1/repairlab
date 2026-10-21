param([string]$Fixture = 'csv-tags')
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    & '.venv\Scripts\python.exe' -m repairlab.cli run --fixture $Fixture --mode demo --backend local --allow-local
    if ($LASTEXITCODE -ne 0) { throw '演示未通过，请查看 .data 中的任务报告。' }
} finally {
    Pop-Location
}
