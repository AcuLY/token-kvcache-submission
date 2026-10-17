# syntax=docker/dockerfile:1.7

FROM node:24-bookworm-slim AS frontend-build
RUN corepack enable
WORKDIR /web
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.11-slim AS python-base
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md ./
COPY shared/ ./shared/
COPY coordinator/ ./coordinator/
COPY agent_peer/ ./agent_peer/
COPY model_service/ ./model_service/
RUN uv sync --frozen --no-dev

FROM python-base AS coordinator
COPY --from=frontend-build /web/dist /app/frontend/dist
ENV FRONTEND_DIST=/app/frontend/dist
CMD ["python", "-m", "coordinator"]

FROM python-base AS agent
CMD ["python", "-m", "agent_peer"]

FROM python-base AS model-service
CMD ["python", "-m", "model_service"]
