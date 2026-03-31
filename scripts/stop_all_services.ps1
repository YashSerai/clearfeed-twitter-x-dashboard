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
foreach ($process in ($workerProcesses | ForEach-Object { $_ })) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    $workerCount += 1
}

$dashboardProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        ($_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe") -and
        $_.CommandLine -and
        $_.CommandLine -like "*run_dashboard.py*"
    }

if (-not $dashboardProcesses) {
$dashboardCount = 0
foreach ($process in ($dashboardProcesses | ForEach-Object { $_ })) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    $dashboardCount += 1
}

if (-not $Quiet) {
    if ($workerCount -gt 0) {
        Write-Host "Stopped $workerCount worker process(es)."
    }
    elseif (-not $dashboardProcesses) {
        Write-Host "No worker process found."
    }

    if ($dashboardCount -gt 0) {
        Write-Host "Stopped $dashboardCount dashboard process(es)."
    }
    elseif (-not $workerProcesses) {
        Write-Host "No dashboard process found."
    }
}
