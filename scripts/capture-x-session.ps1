param(
    [switch]$UseManagedBrowser,
    [ValidateSet("auto", "chrome", "edge")]
    [string]$Browser = "auto"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\\Scripts\\python.exe"

if (-not (Test-Path $python)) {
    throw "Missing .venv. Run .\\scripts\\bootstrap.ps1 first."
}

Set-Location $root

if ($UseManagedBrowser) {
    & $python ".\\scripts\\save_playwright_x_session.py"
} else {
    & ".\\scripts\\launch-real-browser-for-x-session.ps1" -Browser $Browser
    & $python ".\\scripts\\save_playwright_x_session_from_cdp.py"
}
