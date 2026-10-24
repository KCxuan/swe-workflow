param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
. (Join-Path $PSScriptRoot 'runtime.ps1')
$taskHome = Get-WorkflowHome
New-Item -ItemType Directory -Path $taskHome -Force | Out-Null
$taskPython = Join-Path $taskHome '.venv/Scripts/python.exe'

function Check-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE)." }
}

if (-not (Test-Path -LiteralPath $taskPython)) {
    & $Python -m venv (Join-Path $taskHome '.venv')
    Check-Exit 'Create Python environment'
}
& $taskPython -m pip install -r requirements.txt
Check-Exit 'Install Python dependencies'
& $taskPython -m playwright install chromium --only-shell
Check-Exit 'Install Chromium'
if (-not (Test-Path -LiteralPath 'frontend/dist/index.html')) {
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Install Node.js 20.19+ or 22.12+ to build the frontend.' }
    Push-Location -LiteralPath frontend
    try {
        & npm.cmd ci
        Check-Exit 'Install frontend dependencies'
        & npm.cmd run build
        Check-Exit 'Build workbench'
    } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath (Join-Path $taskHome '.env'))) {
    Copy-Item -LiteralPath '.env.example' -Destination (Join-Path $taskHome '.env')
}
Write-Host "Setup complete. Configure $taskHome\.env, then run: .\start.ps1"
