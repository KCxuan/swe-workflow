param([switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) { throw 'Run .\setup.ps1 first.' }
if (-not (Test-Path -LiteralPath 'frontend/dist/index.html')) { throw 'Run .\setup.ps1 to build the workbench first.' }
if (-not (Test-Path -LiteralPath '.env')) { throw 'Copy .env.example to .env and configure the model service.' }
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot '.cache/browsers'
Write-Host 'Workbench: http://127.0.0.1:8765  (Ctrl+C to stop)'
if ($OpenBrowser) { Start-Process 'http://127.0.0.1:8765' }
& ./.venv/Scripts/python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8765
exit $LASTEXITCODE
