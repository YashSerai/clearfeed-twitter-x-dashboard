$ErrorActionPreference = "Stop"

param(
    [switch]$Quiet
)

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

$cloudflared = Get-CloudflaredCommand
if ($cloudflared) {
    if (-not $Quiet) {
        Write-Host "cloudflared is available at $cloudflared"
    }
    exit 0
}

if (-not $Quiet) {
    Write-Host "cloudflared was not found. Attempting installation..."
}

$installed = $false
if (Get-Command winget -ErrorAction SilentlyContinue) {
    try {
        & winget install --id Cloudflare.cloudflared -e --accept-package-agreements --accept-source-agreements
        $installed = $true
    }
    catch {
        if (-not $Quiet) {
            Write-Host "winget install failed: $($_.Exception.Message)"
        }
    }
}

if (-not $installed -and (Get-Command choco -ErrorAction SilentlyContinue)) {
    try {
        & choco install cloudflared -y
        $installed = $true
    }
    catch {
        if (-not $Quiet) {
            Write-Host "choco install failed: $($_.Exception.Message)"
        }
    }
}

$cloudflared = Get-CloudflaredCommand
if (-not $cloudflared) {
    throw "cloudflared is required for Telegram Mini App mode. Install it manually or rerun setup after enabling winget or choco."
}

if (-not $Quiet) {
    Write-Host "cloudflared installed at $cloudflared"
}
