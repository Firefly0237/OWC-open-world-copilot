# owcopilot SaaS image (WS-P). Multi-stage: build the Vue app, then ship it from the FastAPI
# process (one process, one port). The control plane uses Postgres in production; the canon stays
# file-backed under a per-tenant volume.
#
# NOTE: provided as a deployment artifact. It is NOT runtime-verified in the dev sandbox (no Docker
# there) — build/run it on a machine with Docker. The application code it packages IS fully tested.

# --- stage 1: frontend ---
FROM node:20-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- stage 2: runtime ---
FROM python:3.13-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[serve,semantic]"

# the built SPA, served from the API
COPY --from=frontend /app/frontend/dist ./frontend/dist

# per-tenant world storage + control-plane location (mount a volume here in compose)
ENV OWCOPILOT_WORLDS_HOME=/data/worlds \
    OWCOPILOT_PLATFORM_DB=/data/platform.sqlite
# In production set: OWCOPILOT_JWT_SECRET, OWCOPILOT_API_KEY, OPENAI_BASE_URL/API_KEY, and point
# OWCOPILOT_PLATFORM_DB at Postgres via your adapter (see deploy/README.md).
EXPOSE 8000
CMD ["uvicorn", "owcopilot.service.api:app", "--host", "0.0.0.0", "--port", "8000"]
