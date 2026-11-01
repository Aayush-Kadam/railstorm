FROM node:22-bookworm-slim AS frontend
WORKDIR /app/frontend
RUN corepack enable && corepack prepare pnpm@11.25.0 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm run build

FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.9.8@sha256:08f409e1d53e77dfb5b65c788491f8ca70fe1d2d459f41c89afa2fcbef998abe /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock ./
ENV UV_HTTP_TIMEOUT=30 UV_HTTP_RETRIES=0
RUN uv --version && uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
COPY --from=frontend /app/frontend/dist ./frontend/dist
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
EXPOSE 8840
CMD ["sh", "-c", "alembic upgrade head && python scripts/load_scenarios.py && uvicorn railstorm.api:factory --factory --host 0.0.0.0 --port 8840"]
