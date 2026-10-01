FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 依赖层与源码层分离，便于镜像缓存
# extras 必须覆盖平台侧全部可选子系统：少装一个，对应腿就在生产里静默降级
# （analytics 缺 → 分钟物化不写；graph 缺 → 案例知识只有内存兜底；retrieval 缺 → 密集/重排腿不可用）。
# edge（Zenoh）与 perf（locust/schemathesis）不在这里：前者跑在站端，后者只在 CI 里跑。
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN cd backend && uv sync --frozen --no-dev \
    --extra postgres --extra iot --extra analytics --extra graph --extra retrieval

COPY backend ./backend
COPY contracts /contracts
ENV AEGIS_CONTRACTS_DIR=/contracts
# 权重不烧进镜像（两个 int8 模型合计 ~1.1GB）：由 compose 以只读卷挂载 /models，
# 现场用仓库根 scripts/fetch_retrieval_models.py 预置该目录。
ENV AEGIS_RETRIEVAL_MODEL_DIR=/models

RUN useradd --create-home --uid 10001 aegis
USER aegis

WORKDIR /app/backend
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)"]

CMD ["python", "-m", "aegis.main"]
