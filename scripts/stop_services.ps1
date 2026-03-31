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

if (-not $workerProcesses) {
    if (-not $Quiet) {
        Write-Host "No worker process found."
    }
    exit 0
}

$count = 0
foreach ($process in $workerProcesses) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    $count += 1
}

if (-not $Quiet) {
    Write-Host "Stopped $count worker process(es)."
}
