# Create this clone's local .venv and install pinned dependencies.
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
# Then run exports with:
#   .\.venv\Scripts\python.exe -m weread_exporter -b <book-id> ...

$ErrorActionPreference = "Stop"

# Resolve the repository root (parent of scripts/).
function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

# Create .venv if missing and install requirements.txt into it.
function Initialize-ProjectVenv {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RepoRoot
    )

    Set-Location $RepoRoot
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

    if (-not (Test-Path $venvPython)) {
        Write-Host "Creating .venv under $RepoRoot"
        python -m venv .venv
    } else {
        Write-Host "Reusing existing .venv at $RepoRoot\.venv"
    }

    if (-not (Test-Path $venvPython)) {
        throw "Failed to create $venvPython"
    }

    Write-Host "Installing requirements.txt into .venv"
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r (Join-Path $RepoRoot "requirements.txt")

    Write-Host ""
    Write-Host "Done. Run exports with this clone's interpreter only:"
    Write-Host "  $venvPython -m weread_exporter -b <book-id> ..."
}

$root = Get-RepoRoot
Initialize-ProjectVenv -RepoRoot $root
