# syntax=docker/dockerfile:1
# Multi-stage build: build the Vue frontend, install the API into a venv, then copy only the
# built assets + venv into a slim runtime so one image serves the whole single-process product.
# The image is offline-by-default and contains NO secrets — provider keys are injected at runtime
# via env / secrets, never baked into a layer.

# ---------- frontend builder ----------
FROM node:20-slim AS frontend
WORKDIR /app/frontend
# Install deps against the lockfile first so the dependency layer caches well.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build   # -> /app/frontend/dist

# ---------- python builder ----------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Copy only what the package build needs (the metadata + source), so the dependency layer caches
# well. Descriptive docs and project World Bible files stay outside the image/build contract.
COPY pyproject.toml ./
COPY src ./src

# Build a self-contained venv with the deployable API + real-provider SDK. `dev` stays out (tests
# don't ship); the offline deterministic doubles need no extra dependency.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install ".[serve,live]"

# ---------- runtime ----------
FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OWCOPILOT_LLM_MODE=offline \
    OWCOPILOT_ROUTER_MODE=cascade \
    OWCOPILOT_CACHE_MODE=exact+semantic \
    OWCOPILOT_PREFIX_MODE=retrieval \
    OWCOPILOT_RATE_LIMIT_PER_MIN=60 \
    OWCOPILOT_FRONTEND_DIST=/app/frontend/dist
WORKDIR /app

# Non-root user (don't run the service as root).
RUN useradd --create-home --uid 10001 appuser
COPY --from=builder /opt/venv /opt/venv
# The built Vue workbench travels with the image so the single process serves the UI at /.
COPY --from=frontend /app/frontend/dist /app/frontend/dist
USER appuser

EXPOSE 8000

# Container-level healthcheck hits the app's /health (no extra tooling: use python's stdlib).
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)"

CMD ["uvicorn", "owcopilot.service.api:app", "--host", "0.0.0.0", "--port", "8000"]
