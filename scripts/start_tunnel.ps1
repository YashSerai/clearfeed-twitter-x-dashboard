param(
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Get-EnvMap {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path $Path)) {
        return $map
    }
    foreach ($rawLine in Get-Content $Path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            continue
        }
        $parts = $line.Split("=", 2)
        $map[$parts[0].Trim()] = $parts[1].Trim()
    }
    return $map
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

function Get-CloudflaredCommand {
    $cmd = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    $cmd = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    return $null
}

$envPath = Join-Path $root ".env"
$envMap = Get-EnvMap -Path $envPath
$telegramModeRaw = ""
if ($envMap.ContainsKey("TELEGRAM_WEBAPP_ENABLED")) {
    $telegramModeRaw = [string]$envMap["TELEGRAM_WEBAPP_ENABLED"]
}
$autoStartRaw = ""
if ($envMap.ContainsKey("CLOUDFLARED_AUTO_START")) {
    $autoStartRaw = [string]$envMap["CLOUDFLARED_AUTO_START"]
}
$tunnelMode = "quick"
if ($envMap.ContainsKey("CLOUDFLARED_TUNNEL_MODE") -and $envMap["CLOUDFLARED_TUNNEL_MODE"]) {
    $tunnelMode = [string]$envMap["CLOUDFLARED_TUNNEL_MODE"]
}
$telegramMiniAppEnabled = ($telegramModeRaw.ToLowerInvariant() -in @("1", "true", "yes", "on"))
$autoStartTunnel = ($autoStartRaw.ToLowerInvariant() -in @("1", "true", "yes", "on"))
$tunnelMode = $tunnelMode.ToLowerInvariant()

if (-not $telegramMiniAppEnabled -or -not $autoStartTunnel) {
    if (-not $Quiet) {
        Write-Host "Telegram Mini App tunnel is disabled."
    }
    exit 0
}

if ($tunnelMode -ne "quick") {
    throw "Unsupported CLOUDFLARED_TUNNEL_MODE '$tunnelMode'. Supported value: quick"
}

& "$PSScriptRoot\ensure-cloudflared.ps1" -Quiet:$Quiet
$cloudflared = Get-CloudflaredCommand
if (-not $cloudflared) {
    throw "cloudflared is not installed."
}

$logsDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

$stdoutPath = Join-Path $logsDir "cloudflared-startup.out.log"
$stderrPath = Join-Path $logsDir "cloudflared-startup.err.log"
$serviceLogPath = Join-Path $logsDir "cloudflared.log"
foreach ($path in @($stdoutPath, $stderrPath, $serviceLogPath)) {
    if (Test-Path $path) {
        Remove-Item -Force $path
    }
}

$process = Start-Process -FilePath $cloudflared `
    -ArgumentList @("tunnel", "--url", "http://127.0.0.1:8787", "--logfile", $serviceLogPath, "--loglevel", "info", "--no-autoupdate") `
    -WorkingDirectory $root `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -WindowStyle Hidden `
    -PassThru

$publicUrl = $null
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 500
    $process.Refresh()
    if ($process.HasExited) {
        break
    }
    foreach ($path in @($serviceLogPath, $stdoutPath, $stderrPath)) {
        if (-not (Test-Path $path)) {
            continue
        }
        $match = Select-String -Path $path -Pattern 'https://[-a-z0-9]+\.trycloudflare\.com' -AllMatches -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($match -and $match.Matches.Count -gt 0) {
            $publicUrl = $match.Matches[0].Value
            break
        }
    }
    if ($publicUrl) {
        break
    }
}

if (-not $publicUrl) {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    $detail = $null
    foreach ($path in @($stderrPath, $stdoutPath, $serviceLogPath)) {
        if (Test-Path $path) {
            $detail = (Get-Content $path -ErrorAction SilentlyContinue | Select-Object -Last 20) -join " "
            if ($detail.Trim()) {
                break
            }
        }
    }
    throw "cloudflared failed to produce a public URL. $detail"
}

Set-EnvValue -Path $envPath -Key "PUBLIC_BASE_URL" -Value $publicUrl

if (-not $Quiet) {
    Write-Host "Started cloudflared quick tunnel at $publicUrl"
}
