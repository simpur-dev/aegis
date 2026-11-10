FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 依赖层与源码层分离，便于镜像缓存
# extras 必须覆盖平台侧全部可选子系统：少装一个，对应腿就在生产里静默降级
# （analytics 缺 → 分钟物化不写；graph 缺 → 案例知识只有内存兜底；retrieval 缺 → 密集/重排腿不可用；
#  mcp 缺 → `/api/v1/integrations` 的 mcp 行读成 driver=absent，工具面只在 REST 兼容面上）。
# edge（Zenoh）与 perf（locust/schemathesis）不在这里：前者跑在站端，后者只在 CI 里跑。
# seekdb（pymysql）在这里：`.env.example` 把 `AEGIS_RETRIEVAL_INDEX_BACKEND=seekdb` 交给部署面选，
# 容器里没有驱动就等于给运维一个"打开必抛"的开关（缺依赖时 retrieval/seekdb.py 抛
# "seekdb 检索需要 pymysql 依赖"）——一个纯 Python 驱动，几十 KB，不值得让它成为现场惊喜。
# agents（langgraph/langchain）在这里：灾种闭环模块（aegis.agents.hazards）走同一契约，
# 部署面用 `AEGIS_AGENT_IMPL=ext` 选它。**装它不是为了默认启用**——默认仍是 builtin——
# 而是为了让"切实现"这件事在容器形态下也做得到：依赖不在镜像里时，装配点只能如实退回
# builtin 并记 warning，部署方想选 ext 就得自己改镜像构建，那不叫可选。
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN cd backend && uv sync --frozen --no-dev \
    --extra postgres --extra iot --extra analytics --extra graph --extra retrieval --extra seekdb --extra mcp --extra agents

COPY backend ./backend
COPY contracts /contracts
ENV AEGIS_CONTRACTS_DIR=/contracts
# 权重不烧进镜像（两个 int8 模型合计 ~1.1GB）：由 compose 以只读卷挂载 /models，
# 现场用仓库根 scripts/fetch_retrieval_models.py 预置该目录。
ENV AEGIS_RETRIEVAL_MODEL_DIR=/models

RUN useradd --create-home --uid 10001 aegis
USER aegis

WORKDIR /app/backend
# `python` 必须是 uv 建出来的那个虚拟环境里的解释器，且 `src/` 要在导入路径上。
# 实测（本机 `docker build` + `docker run` 出来的镜像）：这两行缺任一行，容器都停在
# `ModuleNotFoundError: No module named 'aegis'`——三个可选腿的依赖其实都装好了（aiomqtt /
# asyncpg / pgvector / clickhouse-connect / duckdb / neo4j+graphiti-core / onnxruntime+tokenizers
# 逐个 import 过），唯独项目自身没进解释器的搜索路径，而 CMD 用的正是系统 `python`。
ENV PATH="/app/backend/.venv/bin:${PATH}" \
    PYTHONPATH=/app/backend/src
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)"]

# 测试/CI 形态：在生产依赖之上补 dev（pytest 等），用于容器内跑全量测试。
# 生产镜像保持上面的 --no-dev 干净形态；构建测试镜像用 `docker build --target test`。
# extras 必须与生产层完全一致再叠加 dev：uv sync 会按本次指定的 extras 收敛，漏写即被卸载。
#
# 【stage 顺序是硬约束】不带 `--target` 的 `docker build` 取的是**最后一个** stage，compose 的
# `build:` 也没写 target——所以生产 stage 必须排在最后。把 test 放在最后会让默认构建产物
# 变成带 dev 的测试镜像，那不是"多一个可选形态"，而是把默认产物换掉了。
FROM base AS test
USER root
# WORKDIR 已继承为 /app/backend（pyproject 所在目录），直接 uv sync。
RUN uv sync --frozen \
    --extra dev --extra postgres --extra iot --extra analytics --extra graph \
    --extra retrieval --extra seekdb --extra mcp --extra agents
USER aegis
# 测试镜像不跑服务：继承生产层的 HEALTHCHECK 会因为 /healthz 无人应答而一直判不健康。
HEALTHCHECK NONE
CMD ["python", "-m", "pytest", "tests", "-q"]

# 生产 stage 放最后：默认 `docker build` 产物必须是它（--no-dev，不含 edge/perf/objectstore）。
FROM base AS production
CMD ["python", "-m", "aegis.main"]
