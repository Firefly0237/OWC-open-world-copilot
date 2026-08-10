# OWCopilot Web workbench

The Web app is a Vue 3 + TypeScript + Vite client for the FastAPI product API. It is not a static
mock: pages use the same workspace, intake, retrieval, Agent, review, export, and Team contracts as
the service. The first-run tour and page-level empty states explain what each surface needs.

## Requirements

- Node.js 22.12 or newer and npm 10 or newer;
- Python 3.11/3.12 with `.[serve]` installed for the API;
- no provider key for the default offline mode.

Install exactly the frontend dependency graph recorded in `package-lock.json`:

```powershell
npm ci --prefix apps/web
```

## Development

From the repository root, use two terminals:

```powershell
# terminal 1: API
.\.venv\Scripts\python -m uvicorn owcopilot.service.api:create_app --factory --port 8000

# terminal 2: Vite
npm run dev --prefix apps/web
```

Open <http://localhost:5173>. The checked-in Vite configuration proxies API routes to
`http://127.0.0.1:8000`, so no hidden frontend `.env` is required. If port 5173 or 8000 is occupied,
stop that process; both ports are intentionally strict so a test cannot silently target another app.

## Production-style local run

```powershell
npm run build --prefix apps/web
.\.venv\Scripts\python -m uvicorn owcopilot.service.api:create_app --factory --port 8000
```

Restart FastAPI after building, then open <http://localhost:8000>. FastAPI discovers
`apps/web/dist/index.html` and mounts the SPA after API routers. A missing build returns a diagnostic
JSON root while `/docs` and `/health` remain available. The Dockerfile performs this build in a
separate Node stage and serves the same SPA/API contract from one non-root container.

## Quality gates

```powershell
npm test --prefix apps/web
npm run typecheck --prefix apps/web
npm run build --prefix apps/web
npm run test:bundle --prefix apps/web
npm run test:browser --prefix apps/web
npm run test:browser:real --prefix apps/web
```

`build` includes `vue-tsc --noEmit`; `test:bundle` enforces the checked-in bundle budget;
`test:browser` drives desktop and mobile smoke paths against a mock API. Run `build` first, then
`test:browser:real` to launch a temporary real Uvicorn/FastAPI service and project, exercise the
same-origin production build, and validate its OpenAPI surface. These gates run in CI without a paid
provider. They do not replace real-provider, real-Unity, or real-Unreal acceptance.

## Team profile

The Team page is useful only against a service started with the PostgreSQL Team configuration and a
valid Bearer identity. Unmigrated Team routes fail closed. Provision the service with the
[Team operator runbook](../../infra/team/README.md); do not add development credentials or JWTs to
frontend source, `localStorage`, screenshots, or commits.

For the clean-machine path, tokenizer provisioning, offline system demo, and live-provider opt-in,
start with the [repository quick start](../../README.md#quick-start) and [`.env.example`](../../.env.example).
