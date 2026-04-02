param(
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

$workerProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        ($_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe") -and
        $_.CommandLine -and
        $_.CommandLine -like "*run_worker.py*"
    }

$workerCount = 0
foreach ($process in @($workerProcesses)) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    $workerCount += 1
}

$dashboardProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        ($_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe") -and
        $_.CommandLine -and
        $_.CommandLine -like "*run_dashboard.py*"
    }

$dashboardCount = 0
foreach ($process in @($dashboardProcesses)) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    $dashboardCount += 1
}

$tunnelProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -like "cloudflared*" -and
        $_.CommandLine -and
        $_.CommandLine -like "*http://127.0.0.1:8787*"
    }

$tunnelCount = 0
foreach ($process in @($tunnelProcesses)) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    $tunnelCount += 1
}

if (-not $Quiet) {
    if ($workerCount -gt 0) {
        Write-Host "Stopped $workerCount worker process(es)."
    }
    else {
        Write-Host "No worker process found."
    }

    if ($dashboardCount -gt 0) {
        Write-Host "Stopped $dashboardCount dashboard process(es)."
    }
    else {
        Write-Host "No dashboard process found."
    }

    if ($tunnelCount -gt 0) {
        Write-Host "Stopped $tunnelCount tunnel process(es)."
    }
    else {
        Write-Host "No tunnel process found."
    }
}
