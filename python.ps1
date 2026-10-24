$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'runtime.ps1')
$taskPython = Join-Path (Get-WorkflowHome) '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Run .\setup.ps1 first.' }
Set-Location -LiteralPath $PSScriptRoot
& $taskPython @args
exit $LASTEXITCODE
