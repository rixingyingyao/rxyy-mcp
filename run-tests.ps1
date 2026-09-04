# rxyy-mcp test suite
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
& $py -m pytest rxyy_mcp\tests -q @args
exit $LASTEXITCODE
