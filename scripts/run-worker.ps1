$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\\Scripts\\python.exe"

if (-not (Test-Path $python)) {
    throw "Missing .venv. Run .\\scripts\\bootstrap.ps1 first."
}

Set-Location $root
& $python ".\\scripts\\run_worker.py"
