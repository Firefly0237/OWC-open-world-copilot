# Contributing

Thank you for improving OWCopilot. Keep changes reviewable, reproducible, and honest about which
execution path was actually tested.

## Setup and gates

Follow the [repository quick start](README.md#quick-start). Before opening a pull request, run the
smallest relevant tests and, for broad changes, the full local gates:

```powershell
.\.venv\Scripts\python -m ruff check src tests scripts
.\.venv\Scripts\python -m mypy src\owcopilot
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python scripts\check_markdown_links.py
npm test --prefix apps/web
npm run build --prefix apps/web
pwsh .\scripts\ci_gate.ps1
```

Use deterministic fakes only at explicit provider boundaries. A fake model can test orchestration,
but its result must not be described as real-model quality. New evaluations need a versioned dataset,
controls, declared splits, machine-readable artifacts, and a reproducible command.

## Dependencies and generated data

- Change Python compatibility windows in `pyproject.toml` only with tests on supported Python lines.
- Commit frontend dependency changes together with `apps/web/package-lock.json`.
- Model ids and immutable revisions belong in `src/owcopilot/model-revisions.json`, with the root
  `model-revisions.json` kept as an exact readable mirror; resolve revisions from the official model
  host and never invent a hash.
- Never commit `.env`, API keys, tokens, runtime databases, model weights/caches, build output, or
  generated benchmark runs.
- Public project guidance belongs in the root README or the README owned by the relevant subsystem.
  The local `docs/` workspace is intentionally excluded from the public Git contract.

## Pull requests

Keep one coherent change per pull request. Describe the user-visible outcome, changed contracts,
tests run, skipped real-system checks, migration/rollback needs, and any cost or security impact.
Update documentation and evidence paths with the implementation. Do not claim a provider, engine,
database, or multi-instance path was tested unless that real boundary was exercised.
