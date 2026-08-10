# Clean-checkout local quality gates in one shot. Any failing gate aborts with a non-zero exit code.
# Install the documented dev/serve/mcp/pdf/tokenizer extras and provision the pinned tokenizer
# before running this script. Hosted models are deliberately absent: every gate is offline and $0.
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

Invoke-Gate "ruff check"       { & $Python -m ruff check src tests scripts }
Invoke-Gate "mypy"             { & $Python -m mypy src\owcopilot }
Invoke-Gate "published links"  { & $Python scripts\check_markdown_links.py }
Invoke-Gate "tokenizer offline" { & $Python scripts\prefetch_models.py --component tokenizer --offline }
Invoke-Gate "pytest"           { & $Python -m pytest -q }
Invoke-Gate "eval-acceptance"  { & $Python -m owcopilot.cli.main eval-acceptance --workspace .tmp\acceptance_ci }
Invoke-Gate "formal artifacts" { & $Python scripts\verify_formal_eval_artifacts.py }
Invoke-Gate "formal runner"    {
    & $Python scripts\run_formal_evals.py `
        --output-dir .tmp\formal-evals-local `
        --run-id local-formal-v1 `
        --generated-at 2026-08-03T00:00:00Z
}
Invoke-Gate "frontend tests"        { npm test --prefix apps/web }
Invoke-Gate "frontend build"        { npm run build --prefix apps/web }
Invoke-Gate "frontend bundle"       { npm run test:bundle --prefix apps/web }
Invoke-Gate "frontend mock browser" { npm run test:browser --prefix apps/web }
Invoke-Gate "frontend real browser" { npm run test:browser:real --prefix apps/web }

Write-Host "ALL GATES PASSED" -ForegroundColor Green
