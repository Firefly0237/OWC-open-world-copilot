# All local quality gates in one shot. Any failing gate aborts with a non-zero exit code.
# Mirrors CI: ruff (lint + format), mypy, pytest, acceptance benchmark, and the frontend
# type-check+build (`npm run build` runs `vue-tsc --noEmit && vite build`).
$ErrorActionPreference = "Stop"

$Python = "python"
if (Test-Path ".\.venv\Scripts\python.exe") {
    $Python = ".\.venv\Scripts\python.exe"
}

function Invoke-Gate([string]$Name, [scriptblock]$Action) {
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) {
        Write-Host "GATE FAILED: $Name (exit $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
}

Invoke-Gate "ruff check"      { & $Python -m ruff check . }
Invoke-Gate "ruff format"     { & $Python -m ruff format --check . }
Invoke-Gate "mypy"            { & $Python -m mypy src }
Invoke-Gate "pytest"          { & $Python -m pytest -q }
Invoke-Gate "eval-acceptance" { & $Python -m owcopilot.cli.main eval-acceptance --workspace .tmp\acceptance_ci }
Invoke-Gate "frontend build"  { npm run build --prefix frontend }

Write-Host "ALL GATES PASSED" -ForegroundColor Green
