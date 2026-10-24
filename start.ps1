param([switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot 'runtime.ps1')
$taskHome = Get-WorkflowHome
$taskPython = Join-Path $taskHome '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Run .\setup.ps1 first.' }
if (-not (Test-Path -LiteralPath 'frontend/dist/index.html')) { throw 'Run .\setup.ps1 to build the workbench first.' }
if (-not (Test-Path -LiteralPath (Join-Path $taskHome '.env'))) { throw "Configure $taskHome\.env first." }
Write-Host 'Workbench: http://127.0.0.1:8765  (Ctrl+C to stop)'
if ($OpenBrowser) { Start-Process 'http://127.0.0.1:8765' }
& $taskPython -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8765
exit $LASTEXITCODE
