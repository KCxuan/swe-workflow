param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Check-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE)." }
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Install Node.js 20.19+ or 22.12+ first.' }
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    & $Python -m venv .venv
    Check-Exit 'Create Python environment'
}
& ./.venv/Scripts/python.exe -m pip install -r requirements.txt
Check-Exit 'Install Python dependencies'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PSScriptRoot '.cache/browsers'
& ./.venv/Scripts/python.exe -m playwright install chromium
Check-Exit 'Install Chromium'
Push-Location -LiteralPath frontend
try {
    & npm.cmd ci
    Check-Exit 'Install frontend dependencies'
    & npm.cmd run build
    Check-Exit 'Build workbench'
} finally { Pop-Location }
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
Write-Host 'Setup complete. Configure .env, then run: .\start.ps1'
