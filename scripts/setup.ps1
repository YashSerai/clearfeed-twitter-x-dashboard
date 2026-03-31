$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\\Scripts\\python.exe"
if (-not (Test-Path $python)) {
    throw "Missing .venv. Run .\\scripts\\bootstrap.ps1 first."
}

function Set-EnvValue {
    param(
        [string]$Path,
        [string]$Key,
        [string]$Value
    )

    $lines = @()
    if (Test-Path $Path) {
        $lines = Get-Content $Path
    }

    $updated = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^$([regex]::Escape($Key))=") {
            $lines[$i] = "$Key=$Value"
            $updated = $true
            break
        }
    }

    if (-not $updated) {
        $lines += "$Key=$Value"
    }

    Set-Content -Path $Path -Value $lines -Encoding UTF8
}

$paths = @(
    ".\\data\\browser",
    ".\\data\\generated",
    ".\\data\\runtime",
    ".\\logs",
    ".\\profiles\\generated"
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
Write-Host "Choose your AI provider:"
Write-Host "  1. Vertex"
Write-Host "  2. OpenAI-compatible"
$providerChoice = (Read-Host "Enter 1 or 2").Trim()
if ($providerChoice -ne "2") {
    $providerChoice = "1"
}

if ($providerChoice -eq "1") {
    Set-EnvValue -Path ".\\.env" -Key "AI_PROVIDER" -Value "vertex"
    Set-EnvValue -Path ".\\.env" -Key "AI_TEXT_MODEL" -Value "gemini-3-flash-preview"
    Set-EnvValue -Path ".\\.env" -Key "AI_POLISH_MODEL" -Value "gemini-3-flash-preview"
    Set-EnvValue -Path ".\\.env" -Key "AI_VISION_MODEL" -Value "gemini-3-flash-preview"
    Set-EnvValue -Path ".\\.env" -Key "AI_IMAGE_MODEL" -Value "gemini-2.5-flash-image"
    Write-Host "Selected provider: Vertex"
    Write-Host "Fill these next:"
    Write-Host "  GOOGLE_CLOUD_PROJECT"
    Write-Host "  GOOGLE_APPLICATION_CREDENTIALS"
    Write-Host "  optional: GOOGLE_CLOUD_LOCATION"
} else {
    Set-EnvValue -Path ".\\.env" -Key "AI_PROVIDER" -Value "openai_compatible"
    Set-EnvValue -Path ".\\.env" -Key "AI_TEXT_MODEL" -Value "your-text-model"
    Set-EnvValue -Path ".\\.env" -Key "AI_POLISH_MODEL" -Value "your-polish-model"
    Set-EnvValue -Path ".\\.env" -Key "AI_VISION_MODEL" -Value ""
    Set-EnvValue -Path ".\\.env" -Key "AI_IMAGE_MODEL" -Value ""
    Set-EnvValue -Path ".\\.env" -Key "OPENAI_COMPAT_BASE_URL" -Value "http://127.0.0.1:11434/v1"
    Set-EnvValue -Path ".\\.env" -Key "OPENAI_COMPAT_API_KEY" -Value ""
    Set-EnvValue -Path ".\\.env" -Key "OPENAI_COMPAT_TIMEOUT_SECONDS" -Value "180"
    Write-Host "Selected provider: OpenAI-compatible"
    Write-Host "Fill these next:"
    Write-Host "  OPENAI_COMPAT_BASE_URL"
    Write-Host "  AI_TEXT_MODEL"
    Write-Host "  AI_POLISH_MODEL"
    Write-Host "Optional:"
    Write-Host "  OPENAI_COMPAT_API_KEY"
    Write-Host "  AI_VISION_MODEL"
    Write-Host "  AI_IMAGE_MODEL"
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
Write-Host ""
Write-Host "Optional next steps:"
Write-Host "  .\\scripts\\capture-x-session.ps1"
Write-Host "  .\\scripts\\run-dashboard.ps1"
Write-Host "  .\\scripts\\run-worker.ps1"
