$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\\Scripts\\python.exe"
if (-not (Test-Path $python)) {
    throw "Missing .venv. Run .\\scripts\\bootstrap.ps1 first."
}

$paths = @(
    ".\\data\\browser",
    ".\\data\\generated",
    ".\\data\\runtime",
    ".\\logs"
)

foreach ($path in $paths) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}

if (-not (Test-Path ".\\.env")) {
    Copy-Item ".\\.env.example" ".\\.env"
    Write-Host "Created .env from .env.example"
} else {
Write-Host ".env already exists"
}

Write-Host ""
& $python ".\\scripts\\bootstrap_db.py"
if ($LASTEXITCODE -ne 0) {
    throw "Database bootstrap failed."
}
Write-Host "Bootstrapped local SQLite database"
Write-Host ""
Write-Host "Setup complete."
Write-Host "Fill in:"
Write-Host "  .env"
Write-Host "  profiles\\templates\\WhoAmI.Questionnaire.md"
Write-Host "  profiles\\templates\\Voice.Questionnaire.md"
Write-Host "Then generate or edit:"
Write-Host "  profiles\\default\\WhoAmI.md"
Write-Host "  profiles\\default\\Voice.md"
