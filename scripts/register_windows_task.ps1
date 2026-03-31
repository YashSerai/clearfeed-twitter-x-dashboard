param(
    [string]$TaskName = "YashXAgentWorker"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $root "scripts\start_services.ps1"

if (-not (Test-Path $scriptPath)) {
    throw "Missing startup script: $scriptPath"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($startupTrigger, $logonTrigger) `
    -Settings $settings `
    -Description "Runs the Clearfeed services at startup or logon." `
    -Force | Out-Null

Write-Host "Registered task: $TaskName"
Write-Host "Action: powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""

