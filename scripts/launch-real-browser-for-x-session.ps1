$ErrorActionPreference = "Stop"

param(
    [ValidateSet("auto", "chrome", "edge")]
    [string]$Browser = "auto",
    [int]$Port = 9222
)

$candidates = @()

if ($Browser -in @("auto", "chrome")) {
    $candidates += @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    )
}

if ($Browser -in @("auto", "edge")) {
    $candidates += @(
        "C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    )
}

$browserPath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $browserPath) {
    throw "Could not find Chrome or Edge. Install one of them or set PLAYWRIGHT_BROWSER_EXECUTABLE for the managed-browser flow."
}

$profileRoot = Join-Path $env:TEMP "x-signal-dashboard-cdp"
New-Item -ItemType Directory -Force -Path $profileRoot | Out-Null

$args = @(
    "--remote-debugging-port=$Port",
    "--user-data-dir=$profileRoot",
    "https://x.com/home"
)

Write-Host "Launching real browser with remote debugging on port $Port"
Write-Host "Browser: $browserPath"
Write-Host "Profile: $profileRoot"
Write-Host "Log into X in that window, then return to the terminal to save the session."

Start-Process -FilePath $browserPath -ArgumentList $args | Out-Null
