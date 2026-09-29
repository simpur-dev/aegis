FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 依赖层与源码层分离，便于镜像缓存
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN cd backend && uv sync --frozen --no-dev --extra postgres --extra iot

COPY backend ./backend
COPY contracts /contracts
ENV AEGIS_CONTRACTS_DIR=/contracts

RUN useradd --create-home --uid 10001 aegis
USER aegis

WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)"]

CMD ["python", "-m", "aegis.main"]
