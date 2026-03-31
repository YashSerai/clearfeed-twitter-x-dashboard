param(
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

& "$PSScriptRoot\stop_services.ps1" -Quiet

$dashboardProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        ($_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe") -and
        $_.CommandLine -and
        $_.CommandLine -like "*run_dashboard.py*"
    }

if (-not $dashboardProcesses) {
    if (-not $Quiet) {
        Write-Host "No dashboard process found."
    }
    exit 0
}

$count = 0
foreach ($process in $dashboardProcesses) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    $count += 1
}

if (-not $Quiet) {
    Write-Host "Stopped $count dashboard process(es)."
}
