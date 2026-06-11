$ErrorActionPreference = "Stop"

$Python = "python"
if (Test-Path ".\.venv\Scripts\python.exe") {
    $Python = ".\.venv\Scripts\python.exe"
}

& $Python -m pytest
& $Python -m ruff check src tests
& $Python -m mypy src\owcopilot
