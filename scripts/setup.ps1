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

function Get-EnvValue {
    param(
        [string]$Path,
        [string]$Key
    )

    if (-not (Test-Path $Path)) {
        return ""
    }

    foreach ($rawLine in Get-Content $Path) {
        if ($rawLine -match "^$([regex]::Escape($Key))=(.*)$") {
            return $Matches[1]
        }
    }

    return ""
}

$paths = @(
    ".\\data\\browser",
    ".\\data\\generated",
    ".\\data\\runtime",
    ".\\logs",
    ".\\profiles\\generated",
    ".\\profiles\\history",
    ".\\profiles\\local"
)

foreach ($path in $paths) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}

$profilePairs = @(
    @{ Source = ".\\profiles\\default\\WhoAmI.md"; Target = ".\\profiles\\local\\WhoAmI.md" },
    @{ Source = ".\\profiles\\default\\Voice.md"; Target = ".\\profiles\\local\\Voice.md" },
    @{ Source = ".\\profiles\\default\\Humanizer.md"; Target = ".\\profiles\\local\\Humanizer.md" }
)

foreach ($pair in $profilePairs) {
    if (-not (Test-Path $pair.Target) -and (Test-Path $pair.Source)) {
        Copy-Item $pair.Source $pair.Target
        Write-Host "Created $($pair.Target) from starter template"
    }
}

if (-not (Test-Path ".\\.env")) {
    Copy-Item ".\\.env.example" ".\\.env"
    Write-Host "Created .env from .env.example"
} else {
    Write-Host ".env already exists"
    Write-Host "Setup will update provider-related keys in your existing .env."
    Write-Host "It does not replace the whole file, but you should still back up one-time secrets before rerunning setup."
}

if (-not (Select-String -Path ".\\.env" -Pattern "^PUBLIC_BASE_URL=" -Quiet)) {
    Set-EnvValue -Path ".\\.env" -Key "PUBLIC_BASE_URL" -Value ""
}
if (-not (Select-String -Path ".\\.env" -Pattern "^TELEGRAM_WEBAPP_ENABLED=" -Quiet)) {
    Set-EnvValue -Path ".\\.env" -Key "TELEGRAM_WEBAPP_ENABLED" -Value "false"
}
if (-not (Select-String -Path ".\\.env" -Pattern "^TELEGRAM_LEGACY_FORWARDING_ENABLED=" -Quiet)) {
    Set-EnvValue -Path ".\\.env" -Key "TELEGRAM_LEGACY_FORWARDING_ENABLED" -Value "false"
}
if (-not (Select-String -Path ".\\.env" -Pattern "^CLOUDFLARED_AUTO_START=" -Quiet)) {
    Set-EnvValue -Path ".\\.env" -Key "CLOUDFLARED_AUTO_START" -Value "false"
}
if (-not (Select-String -Path ".\\.env" -Pattern "^CLOUDFLARED_TUNNEL_MODE=" -Quiet)) {
    Set-EnvValue -Path ".\\.env" -Key "CLOUDFLARED_TUNNEL_MODE" -Value "quick"
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
    $providerSummary = @(
        "Selected provider: Vertex"
        "Fill these next in .env:"
        "  AI_PROVIDER=vertex"
        "  AI_TEXT_MODEL"
        "  AI_POLISH_MODEL"
        "  optional: AI_VISION_MODEL"
        "  optional: AI_IMAGE_MODEL"
        "  GOOGLE_CLOUD_PROJECT"
        "  GOOGLE_APPLICATION_CREDENTIALS"
        "  optional: GOOGLE_CLOUD_LOCATION"
    )
} else {
    Set-EnvValue -Path ".\\.env" -Key "AI_PROVIDER" -Value "openai_compatible"
    Set-EnvValue -Path ".\\.env" -Key "AI_TEXT_MODEL" -Value "your-text-model"
    Set-EnvValue -Path ".\\.env" -Key "AI_POLISH_MODEL" -Value "your-polish-model"
    Set-EnvValue -Path ".\\.env" -Key "AI_VISION_MODEL" -Value ""
    Set-EnvValue -Path ".\\.env" -Key "AI_IMAGE_MODEL" -Value ""
    Set-EnvValue -Path ".\\.env" -Key "OPENAI_COMPAT_BASE_URL" -Value "http://127.0.0.1:11434/v1"
    Set-EnvValue -Path ".\\.env" -Key "OPENAI_COMPAT_API_KEY" -Value ""
    Set-EnvValue -Path ".\\.env" -Key "OPENAI_COMPAT_TIMEOUT_SECONDS" -Value "180"
    $providerSummary = @(
        "Selected provider: OpenAI-compatible"
        "Fill these next in .env:"
        "  AI_PROVIDER=openai_compatible"
        "  OPENAI_COMPAT_BASE_URL"
        "  AI_TEXT_MODEL"
        "  AI_POLISH_MODEL"
        "  optional: OPENAI_COMPAT_API_KEY"
        "  optional: AI_VISION_MODEL"
        "  optional: AI_IMAGE_MODEL"
        "  optional: OPENAI_COMPAT_TIMEOUT_SECONDS"
    )
}

Write-Host ""
Write-Host "Choose your Telegram access mode:"
Write-Host "  1. Off"
Write-Host "  2. Telegram Mini App with automatic tunnel"
$telegramChoice = (Read-Host "Enter 1 or 2").Trim()
if ($telegramChoice -ne "2") {
    $telegramChoice = "1"
}

if ($telegramChoice -eq "2") {
    Set-EnvValue -Path ".\\.env" -Key "TELEGRAM_WEBAPP_ENABLED" -Value "true"
    Set-EnvValue -Path ".\\.env" -Key "TELEGRAM_LEGACY_FORWARDING_ENABLED" -Value "false"
    Set-EnvValue -Path ".\\.env" -Key "CLOUDFLARED_AUTO_START" -Value "true"
    Set-EnvValue -Path ".\\.env" -Key "CLOUDFLARED_TUNNEL_MODE" -Value "quick"

    $currentBotToken = Get-EnvValue -Path ".\\.env" -Key "TELEGRAM_BOT_TOKEN"
    $currentChatId = Get-EnvValue -Path ".\\.env" -Key "TELEGRAM_CHAT_ID"

    Write-Host ""
    if ($currentBotToken) {
        $botTokenInput = (Read-Host "Telegram bot token (press Enter to keep existing value)").Trim()
    }
    else {
        $botTokenInput = (Read-Host "Telegram bot token (leave blank to fill later)").Trim()
    }
    if ($botTokenInput) {
        Set-EnvValue -Path ".\\.env" -Key "TELEGRAM_BOT_TOKEN" -Value $botTokenInput
    }

    if ($currentChatId) {
        $chatIdInput = (Read-Host "Telegram chat ID (press Enter to keep existing value)").Trim()
    }
    else {
        $chatIdInput = (Read-Host "Telegram chat ID (leave blank to fill later)").Trim()
    }
    if ($chatIdInput) {
        Set-EnvValue -Path ".\\.env" -Key "TELEGRAM_CHAT_ID" -Value $chatIdInput
    }

    try {
        & "$PSScriptRoot\\ensure-cloudflared.ps1"
    }
    catch {
        Write-Host "cloudflared install/setup failed: $($_.Exception.Message)"
        Write-Host "You can still continue, but start_services.ps1 will fail until cloudflared is installed."
    }

    $publicBaseUrl = ""
    try {
        Write-Host ""
        Write-Host "Starting the Cloudflare quick tunnel now so setup can populate PUBLIC_BASE_URL..."
        & "$PSScriptRoot\\start_tunnel.ps1"
        $publicBaseUrl = Get-EnvValue -Path ".\\.env" -Key "PUBLIC_BASE_URL"
    }
    catch {
        Write-Host "Automatic tunnel startup failed during setup: $($_.Exception.Message)"
        Write-Host "You can still continue. start_services.ps1 will retry the tunnel launch later."
    }

    $missingTelegramKeys = @()
    if (-not (Get-EnvValue -Path ".\\.env" -Key "TELEGRAM_BOT_TOKEN")) {
        $missingTelegramKeys += "  TELEGRAM_BOT_TOKEN"
    }
    if (-not (Get-EnvValue -Path ".\\.env" -Key "TELEGRAM_CHAT_ID")) {
        $missingTelegramKeys += "  TELEGRAM_CHAT_ID"
    }

    $telegramSummary = @(
        "Selected Telegram mode: Mini App over tunnel"
        "What setup did:"
        "  enabled Telegram Mini App mode"
        "  disabled legacy Telegram forwarding"
        "  enabled automatic cloudflared startup"
    )

    if ($publicBaseUrl) {
        $telegramSummary += "  populated PUBLIC_BASE_URL=$publicBaseUrl"
        $telegramSummary += "  Telegram Mini App URL: $publicBaseUrl/mini"
    }
    else {
        $telegramSummary += "  did not populate PUBLIC_BASE_URL yet"
    }

    if ($missingTelegramKeys.Count -gt 0) {
        $telegramSummary += "Fill these next in .env:"
        $telegramSummary += $missingTelegramKeys
    }

    $telegramSummary += @(
        "What happens when you start services:"
        "  start_services.ps1 launches a cloudflared quick tunnel automatically"
        "  PUBLIC_BASE_URL is refreshed automatically from that tunnel"
        "  Telegram opens Clearfeed through the menu button and Mini App"
    )

    if ($publicBaseUrl) {
        $telegramSummary += @(
            "Telegram bot setup:"
            "  Mini App link for BotFather or manual bot setup: $publicBaseUrl/mini"
            "  Clearfeed also tries to sync the bot menu button automatically when services start"
        )
    }
}
else {
    Set-EnvValue -Path ".\\.env" -Key "TELEGRAM_WEBAPP_ENABLED" -Value "false"
    Set-EnvValue -Path ".\\.env" -Key "TELEGRAM_LEGACY_FORWARDING_ENABLED" -Value "false"
    Set-EnvValue -Path ".\\.env" -Key "CLOUDFLARED_AUTO_START" -Value "false"
    Set-EnvValue -Path ".\\.env" -Key "CLOUDFLARED_TUNNEL_MODE" -Value "quick"
    $telegramSummary = @(
        "Selected Telegram mode: Off"
        "Clearfeed will run locally only until you re-enable Telegram Mini App mode in setup or by editing .env."
    )
}

Write-Host ""
& $python ".\\scripts\\bootstrap_db.py"
if ($LASTEXITCODE -ne 0) {
    throw "Database bootstrap failed."
}
Write-Host "Bootstrapped local SQLite database"
Write-Host ""
Write-Host "Setup complete."
Write-Host ""
foreach ($line in $providerSummary) {
    Write-Host $line
}
Write-Host ""
foreach ($line in $telegramSummary) {
    Write-Host $line
}
Write-Host ""
Write-Host "Profile setup:"
Write-Host "  Fill profiles\\templates\\WhoAmI.Questionnaire.md"
Write-Host "  Fill profiles\\templates\\Voice.Questionnaire.md"
Write-Host "  Then generate or edit:"
Write-Host "    profiles\\local\\WhoAmI.md"
Write-Host "    profiles\\local\\Voice.md"
Write-Host "    profiles\\local\\Humanizer.md"
Write-Host "  These local profile files are ignored by git."
Write-Host ""
Write-Host "Runtime:"
Write-Host "  If your X session is missing or stale: .\\scripts\\capture-x-session.ps1"
Write-Host "  Start dashboard: .\\scripts\\run-dashboard.ps1"
Write-Host "  Start worker: .\\scripts\\run-worker.ps1"
Write-Host "  Or both: .\\scripts\\start_services.ps1"
Write-Host "  If Telegram Mini App mode is enabled, start_services.ps1 also starts the tunnel for you."
Write-Host "  Clearfeed drafts locally. Copy finished posts to X and publish manually."
Write-Host ""
Write-Host "Archive bootstrap:"
Write-Host "  Import an unzipped X archive: .\\scripts\\import-x-archive.ps1 -ArchiveDir \"C:\\path\\to\\twitter-archive\""
Write-Host "  Or import it from the dashboard after startup."
