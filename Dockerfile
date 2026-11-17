# syntax=docker/dockerfile:1
FROM node:22-slim AS frontend
WORKDIR /app/frontend
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/huggingface
WORKDIR /app
COPY pyproject.toml README.md LICENSE model-revisions.json ./
COPY src ./src
COPY scripts/prefetch_models.py ./scripts/prefetch_models.py
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install ".[serve,live,tokenizer]" \
 && /opt/venv/bin/python scripts/prefetch_models.py --component tokenizer \
 && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
      /opt/venv/bin/python scripts/prefetch_models.py --component tokenizer --offline \
 && chmod -R a-w /opt/huggingface

FROM python:3.12-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    OWCOPILOT_LLM_MODE=real \
    OWCOPILOT_CACHE_MODE=exact \
    OWCOPILOT_RATE_LIMIT_PER_MIN=60 \
    OWCOPILOT_FRONTEND_DIST=/app/frontend/dist
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /app/var /app/scripts \
 && chown -R 10001:10001 /app/var
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/huggingface /opt/huggingface
COPY --from=frontend /app/frontend/dist /app/frontend/dist
COPY scripts/serve.py /app/scripts/serve.py
USER appuser
VOLUME ["/app/var"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
CMD ["python", "scripts/serve.py", "--host", "0.0.0.0", "--port", "8000", "--data-dir", "/app/var"]
