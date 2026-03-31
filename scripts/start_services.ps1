$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logsDir = Join-Path $root "logs"

function Read-StartupDetail {
    param(
        [string]$StdErrPath,
        [string]$StdOutPath
    )

    if (Test-Path $StdErrPath) {
        $stderr = (Get-Content $StdErrPath -ErrorAction SilentlyContinue | Select-Object -Last 20) -join " "
        if ($stderr.Trim()) {
            return $stderr.Trim()
        }
    }

    if (Test-Path $StdOutPath) {
        $stdout = (Get-Content $StdOutPath -ErrorAction SilentlyContinue | Select-Object -Last 20) -join " "
        if ($stdout.Trim()) {
            return $stdout.Trim()
        }
    }

    return $null
}

function Start-ManagedPythonService {
    param(
        [string]$Label,
        [string]$ScriptPath,
        [string]$StdOutPath,
        [string]$StdErrPath
    )

    if (Test-Path $StdOutPath) {
        Remove-Item -Force $StdOutPath
    }
    if (Test-Path $StdErrPath) {
        Remove-Item -Force $StdErrPath
    }

    $process = Start-Process -FilePath $python `
        -ArgumentList $ScriptPath `
        -WorkingDirectory $root `
        -RedirectStandardOutput $StdOutPath `
        -RedirectStandardError $StdErrPath `
        -WindowStyle Hidden `
        -PassThru

    Start-Sleep -Seconds 3
    $process.Refresh()

    if (-not $process.HasExited) {
        return @{
            started = $true
            detail = $null
        }
    }

    $detail = Read-StartupDetail -StdErrPath $StdErrPath -StdOutPath $StdOutPath
    if (-not $detail) {
        $detail = "$Label exited during startup with code $($process.ExitCode)."
    }

    return @{
        started = $false
        detail = $detail
    }
}

& "$PSScriptRoot\stop_all_services.ps1" -Quiet

if (-not (Test-Path $python)) {
    throw "Missing .venv. Run .\scripts\bootstrap.ps1 first."
}

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

$dashboardResult = Start-ManagedPythonService `
    -Label "Dashboard" `
    -ScriptPath ".\scripts\run_dashboard.py" `
    -StdOutPath (Join-Path $logsDir "dashboard-startup.out.log") `
    -StdErrPath (Join-Path $logsDir "dashboard-startup.err.log")

$workerResult = Start-ManagedPythonService `
    -Label "Worker" `
    -ScriptPath ".\scripts\run_worker.py" `
    -StdOutPath (Join-Path $logsDir "worker-startup.out.log") `
    -StdErrPath (Join-Path $logsDir "worker-startup.err.log")

if ($dashboardResult.started -and $workerResult.started) {
    Write-Host "Started dashboard and worker."
    exit 0
}

if (-not $dashboardResult.started) {
    Write-Host "Dashboard failed to start: $($dashboardResult.detail)"
}

if (-not $workerResult.started) {
    Write-Host "Worker failed to start: $($workerResult.detail)"
}

exit 1
