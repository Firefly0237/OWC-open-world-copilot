# owcopilot team image. The same artifact runs Alembic with the migration role and the API with
# the non-owner PostgreSQL runtime role; local compatibility remains available as a separate
# deployment profile.

# --- stage 1: frontend ---
FROM node:22-slim AS frontend
WORKDIR /app/frontend
COPY apps/web/package*.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

# --- stage 2: runtime ---
FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/huggingface

COPY pyproject.toml README.md LICENSE model-revisions.json ./
COPY src/ ./src/
COPY alembic.ini ./
COPY infra/migrations/ ./infra/migrations/
COPY scripts/prefetch_models.py ./scripts/prefetch_models.py
COPY scripts/bootstrap_team_genesis.py ./scripts/bootstrap_team_genesis.py
COPY scripts/seed_team_scope.py ./scripts/seed_team_scope.py
RUN pip install --no-cache-dir ".[serve,postgres,tokenizer]" \
 && python scripts/prefetch_models.py --component tokenizer \
 && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
      python scripts/prefetch_models.py --component tokenizer --offline \
 && chmod -R a-w /opt/huggingface

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# the built SPA, served from the API
COPY --from=frontend /app/frontend/dist ./apps/web/dist

# The team profile receives its app-role DSN and JWT secret at runtime; no secret is built in.
ENV OWCOPILOT_PROFILE=team
RUN useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /opt/huggingface
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)"
CMD ["uvicorn", "owcopilot.service.api:app", "--host", "0.0.0.0", "--port", "8000"]
