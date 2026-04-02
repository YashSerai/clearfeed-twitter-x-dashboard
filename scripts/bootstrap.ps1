$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\\Scripts\\python.exe"
if (-not (Test-Path $python)) {
    try {
        py -3.11 -m venv .venv
    } catch {
        try {
            py -3 -m venv .venv
        } catch {
            python -m venv .venv
        }
    }
}

& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt
& $python -m playwright install chromium

Write-Host ""
Write-Host "Bootstrap complete."
Write-Host "Next:"
Write-Host "  1. .\\scripts\\setup.ps1"
Write-Host "     setup.ps1 can also install cloudflared if you enable Telegram Mini App mode"
Write-Host "  2. Fill in .env and profile files"
Write-Host "  3. .\\scripts\\capture-x-session.ps1"
Write-Host "  4. .\\scripts\\run-dashboard.ps1"
