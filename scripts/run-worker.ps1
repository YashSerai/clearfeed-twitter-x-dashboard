$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\\Scripts\\python.exe"
$errorLog = Join-Path $root "logs\\worker-launch-error.log"
$consoleLog = Join-Path $root "logs\\worker-console.log"

try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $errorLog) | Out-Null

    if (Test-Path $errorLog) {
        Remove-Item -Force $errorLog
    }
    if (Test-Path $consoleLog) {
        Remove-Item -Force $consoleLog
    }

    if (-not (Test-Path $python)) {
        throw "Missing .venv. Run .\\scripts\\bootstrap.ps1 first."
    }

    Set-Location $root
    $startedAt = Get-Date
    & $python ".\\scripts\\run_worker.py" *> $consoleLog
    $exitCode = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    $elapsedSeconds = [math]::Round(((Get-Date) - $startedAt).TotalSeconds, 1)

    if ($exitCode -ne 0) {
        throw "Worker exited with code $exitCode after ${elapsedSeconds}s."
    }

    if ($elapsedSeconds -lt 15) {
        throw "Worker exited too quickly after ${elapsedSeconds}s."
    }
}
catch {
    $logTail = $null
    if (Test-Path $consoleLog) {
        $logTail = (Get-Content $consoleLog -ErrorAction SilentlyContinue | Select-Object -Last 20) -join [Environment]::NewLine
    }

    $message = @(
        "Worker launch failed."
        $_.Exception.Message
        if ($logTail) { "" }
        if ($logTail) { "Recent output:" }
        if ($logTail) { $logTail }
    ) -join [Environment]::NewLine
    Set-Content -Path $errorLog -Value $message -Encoding UTF8
    exit 1
}
