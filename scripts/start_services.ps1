$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$dashboardErrorLog = Join-Path $root "logs\\dashboard-launch-error.log"
$workerErrorLog = Join-Path $root "logs\\worker-launch-error.log"

function Wait-ForPythonProcess {
    param(
        [string]$Pattern,
        [int]$TimeoutSeconds = 6
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                ($_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe") -and
                $_.CommandLine -and
                $_.CommandLine -like $Pattern
            }
        if ($processes) {
            return $true
        }
        Start-Sleep -Milliseconds 400
    } while ((Get-Date) -lt $deadline)

    return $false
}

function Read-LaunchError {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        return $null
    }
    $content = Get-Content $Path -ErrorAction SilentlyContinue
    if (-not $content) {
        return $null
    }
    return ($content | Select-Object -First 3) -join " "
}

& "$PSScriptRoot\stop_all_services.ps1" -Quiet

if (Test-Path $dashboardErrorLog) {
    Remove-Item -Force $dashboardErrorLog
}
if (Test-Path $workerErrorLog) {
    Remove-Item -Force $workerErrorLog
}

Start-Process powershell -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $PSScriptRoot "run-dashboard.ps1")
) -WorkingDirectory $root | Out-Null

$dashboardStarted = Wait-ForPythonProcess -Pattern "*run_dashboard.py*"

Start-Process powershell -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    (Join-Path $PSScriptRoot "run-worker.ps1")
) -WorkingDirectory $root | Out-Null

$workerStarted = Wait-ForPythonProcess -Pattern "*run_worker.py*"

if ($dashboardStarted -and $workerStarted) {
    Write-Host "Started dashboard and worker in separate PowerShell windows."
    exit 0
}

if (-not $dashboardStarted) {
    $dashboardError = Read-LaunchError -Path $dashboardErrorLog
    if ($dashboardError) {
        Write-Host "Dashboard failed to start: $dashboardError"
    } else {
        Write-Host "Dashboard failed to start. Check logs\\dashboard-launch-error.log"
    }
}

if (-not $workerStarted) {
    $workerError = Read-LaunchError -Path $workerErrorLog
    if ($workerError) {
        Write-Host "Worker failed to start: $workerError"
    } else {
        Write-Host "Worker failed to start. Check logs\\worker-launch-error.log"
    }
}

exit 1
