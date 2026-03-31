$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot

& "$PSScriptRoot\stop_all_services.ps1" -Quiet

Start-Process powershell -ArgumentList @(
    "-NoProfile",
    "-NoExit",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $PSScriptRoot "run-dashboard.ps1")
) -WorkingDirectory $root | Out-Null

Start-Sleep -Seconds 1

Start-Process powershell -ArgumentList @(
    "-NoProfile",
    "-NoExit",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $PSScriptRoot "run-worker.ps1")
) -WorkingDirectory $root | Out-Null

Write-Host "Started dashboard and worker in separate PowerShell windows."
Write-Host "Those windows now stay open if startup fails, so you can see the error instead of the shell closing immediately."
