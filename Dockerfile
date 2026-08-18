# syntax=docker/dockerfile:1.7

FROM oven/bun:1.3.14 AS web-builder

WORKDIR /app/web

COPY web/package.json web/bun.lock ./
RUN bun install --frozen-lockfile

COPY web/ ./
RUN bun run build


FROM python:3.13-slim AS python-builder

COPY --from=ghcr.io/astral-sh/uv:0.11.33 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project


FROM python:3.13-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 account-import \
    && useradd --uid 10001 --gid account-import --no-create-home account-import \
    && mkdir -p /app/data \
    && chown account-import:account-import /app/data

COPY --from=python-builder /app/.venv /app/.venv
COPY app/ ./app/
COPY redeem_api_sdk.py ./
COPY --from=web-builder /app/web/dist ./web/dist

LABEL org.opencontainers.image.source="https://github.com/yiranxiaohui/account-import" \
      org.opencontainers.image.description="兑换码找回、额度下载与 Sub2API 账号导入工作台"

USER account-import

EXPOSE 8000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
