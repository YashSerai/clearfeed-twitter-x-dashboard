param(
    [Parameter(Mandatory = $true)]
    [string]$ArchiveDir,
    [switch]$RunVoiceBuild
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\\Scripts\\python.exe"
if (-not (Test-Path $python)) {
    throw "Missing .venv. Run .\\scripts\\bootstrap.ps1 first."
}

$args = @(".\\scripts\\import_x_archive.py", "--archive-dir", $ArchiveDir)
if ($RunVoiceBuild) {
    $args += "--run-voice-build"
}

& $python @args
