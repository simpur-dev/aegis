# AEGIS

**Adaptive Emergency Geo-hazard Intelligence System**
西藏山地灾害多智能体协同调控技术与平台 —— 平台侧（后端 + Web）

面向西藏高原 5 类典型山地灾害（滑坡、崩塌危岩、泥石流、冰雪雪崩、冰湖溃决），
构建"感知—研判—决策—执行—反馈"全链条协同调控平台。

- 应用形态：**纯 Web 平台**（响应式，不含原生移动端）
- 分工边界：**平台侧**（L1 接入 / L2 数据 / L4 服务 / L5 应用 / 基座）与 **L3 五大智能体本体**
  均由本仓库承载——智能体自建于 `backend/src/aegis/agents/`（agentkit 契约壳 + LLM 内环），
  按 `contracts/AGENT_INTEGRATION_SPEC.v1.md` 接入总线；外部团队的实现按同一契约接入，`agents/` 是交付落点
- 架构依据：五层+一基座（实现态见 `docs/architecture.md`），融合"一张图 + 一总线 + 四预"扩展；
  LLM 智能体化升级（对齐 MA-RIAEW）见 `docs/课题6_架构升级方案.md`，决策记录 `docs/adr/0007`
- 理论框架：MA-RIAEW（多智能体协同赋能的突发事件风险情报感知及预警模式）
- 所有验收数字与"还没测到的"都记在 `docs/REPORT.md`（含可复跑的取证命令），不写进本文件

---

## 1. 目录结构

```
aegis/
├── contracts/                 # 平台与智能体的唯一接口契约（版本化）
│   ├── agent_message.v1.schema.json
│   ├── stu.v1.schema.json
│   └── AGENT_INTEGRATION_SPEC.v1.md
├── agents/                    # L3 智能体的交付落点与接入指引（外部实现按契约接入；自建本体在 backend/src/aegis/agents/）
├── backend/
│   ├── src/aegis/
│   │   ├── config.py          # 运行配置与考核指标阈值口径（唯一配置面）
│   │   ├── integrations.py    # 可选子系统的唯一装配点：内核不 import 任何可选后端
│   │   ├── container.py       # 平台装配与指标量测出口
│   │   ├── logging.py         # 结构化 JSON 日志 + trace_id 上下文
│   │   ├── errors.py          # 类型化错误码（与规范 §5 对齐）
│   │   ├── domain/            # 枚举与契约的 Python 镜像（AgentMessage / STU / 领域记录）
│   │   ├── bus/               # 总线传输抽象、内存实现、NATS JetStream 实现、网关、能力注册中心
│   │   ├── connectors/        # 数据接入：模拟场站、公开气象 API、MQTT 推送腿、摄取服务
│   │   ├── storage/           # 运行态存储协议与内存实现（遥测/预警/任务/链路）
│   │   ├── persistence/       # PostgreSQL+PostGIS+pgvector 实现、幂等 DDL、写缓冲与回放量测
│   │   ├── analytics/         # 分析旁路：ClickHouse 分钟物化 / DuckDB 边缘离线分析
│   │   ├── knowledge/         # 预案案例库 + Graphiti 时序图谱（读路径不含 LLM）
│   │   ├── retrieval/         # 混合检索：bge-m3 + reranker（ONNX int8 CPU），缺权重时按腿降级
│   │   ├── pipeline/          # 灾害响应链路（智能体优先、平台降级）
│   │   ├── services/          # 触发规则、风险定级、任务拆解、三路融合解析、语义交互（助手，含意图解构）、预警生成、靶向触达、阈值标定；LLM 网关 v2（多模型路由/结构化输出/用量账本/Prompt 注册表）、记忆服务、推演沙盒、提示注入围栏、数字防幻觉闸、质量评测与模型卡片、数据飞轮
│   │   ├── workflow/          # 自研 DAG 引擎（16 类节点）与 5 灾种内置模板（含案例驱动推演节点）
│   │   ├── edge/              # 站端↔网关弱网链路（Eclipse Zenoh POC：有界缓冲 + 按序重放）
│   │   ├── observability/     # 时延账本、OpenTelemetry 埋点与导出、Prometheus 导出
│   │   ├── agents/            # 五大智能体本体：agentkit 契约壳 + LLM 内环 + 三态运行时（real 与 mock 互斥）；mock 留作契约门禁对照
│   │   └── api/               # FastAPI HTTP/SSE 接口、工作流接口、语义交互（助手 SSE + 人工确认）
│   ├── scripts/               # drill / metrics_report / accuracy_replay / eval_report_parsing / quality_gate / emit_model_cards / load_curve / zenoh_poc / import_stations / ingest_cases
│   ├── tests/                 # unit / contract / integration / e2e / api / perf / load
│   └── pyproject.toml         # extras：postgres / iot / graph / retrieval / analytics / edge / dev
├── frontend/                  # Vue 3 + Vite；Cesium 一张图、Vue Flow 编排画布
├── deploy/                    # docker-compose（含 profile）、镜像构建、Prometheus/Alertmanager 配置
├── docs/                      # 实现态架构、架构升级方案（MA-RIAEW 对齐）、ADR、实测记录（REPORT.md）、选型与许可证清单
└── scripts/                   # 检索权重预置脚本（内网/离线部署前跑一次）
```

## 2. 部署与运行

两条互不依赖的路径，命令都在本机逐条跑过（2026-10-12，Windows 11 + Docker Desktop/WSL2）：

- **路径 A · 本机进程**（日常开发、离线全测试、演练与报表）——不需要 Docker。
- **路径 B · Docker 起后端与依赖**（真总线 + 真库 + 图谱 + 检索权重），**Web 端仍在宿主机跑**：
  `deploy/docker-compose.yml` 里没有前端服务，容器只编排后端与其依赖（如实写清，见 2.2 末）。

### 2.0 先决条件（本机实测版本）

| 需要 | 实测到的版本 | 说明 |
| --- | --- | --- |
| `uv` | 0.12.19 | 后端唯一依赖管理器；它会自建虚拟环境，**不必**先装 Python |
| Python | 虚拟环境里 3.12.13（`requires-python = ">=3.11"`） | 系统里那台 `python -V` 是 3.13.15，与项目无关，别用它跑 `uv run` |
| `node` / `npm` | v24.11.1 / 11.6.2 | 前端与仓库根的 `npm run dev` 编排 |
| `docker` + `docker compose` | 29.8.1 / v5.5.1 | 路径 B 才需要；compose 版本要支持 `--env-file` 与 `profiles` |
| 磁盘 | 镜像合计 ~1.6GB（后端 688MB + postgres 892MB）＋检索权重 1.1GB | 权重不烧进镜像，靠只读卷挂进容器（见 2.2） |
| 网络 | 构建期需要 pypi/apt 可达 | 内网/离线见 2.2 的"离线或内网"一段 |

### 2.1 路径 A · 本机进程

```bash
# 1) 配置：LLM 与图谱项可留空，平台按规则引擎与内存案例库降级运行
#    注意：`Settings` 的 env_file 按进程工作目录解析，`backend/.env` 会被 pytest 一起读走。
#    本机实验请用一次性环境变量，别写进 .env——否则"全绿"量到的是你个人的机器偏好：
#      AEGIS_RETRIEVAL_ENABLED=true AEGIS_RETRIEVAL_INDEX_BACKEND=seekdb uv run python -m aegis.main
cp .env.example backend/.env

# 2) 依赖（三处：仓库根的 dev 编排、后端 uv、前端 npm）
npm install                       # 根：只装 concurrently 一个开发依赖，专为下面那条 `npm run dev`
cd backend && uv sync --extra dev
#    ↑ 装 dev 就能把服务跑起来，但**跑不齐门禁**：可选子系统各自的驱动在别的 extra 里
#      （aiomqtt 在 iot、pymysql 在 seekdb、asyncpg/pgvector 在 postgres……）。
#      要把整棵 tests/ 跑到与 CI 同集合，用下面这一句（`--extra` 是整表替换，必须一次列全）：
#      uv sync --extra dev --extra perf --extra postgres --extra iot --extra analytics \
#              --extra retrieval --extra edge --extra graph --extra seekdb --extra mcp
cd ../frontend && npm install

# 3) 全量测试：范围是 tests/ 整棵树（含 tests/perf 与 tests/integration）
#    只跑 tests/unit 那一档子集不算门禁：2026-10-12 就是"本机子集全绿、CI 红"，
#    红在一个本机从没进过命令的 perf 用例上。
#    与 CI 的同集合=三条：`--frozen` 装齐全 extras、`PYTHONHASHSEED=0`（hypothesis 那条随 seed 抖，
#    mock 的随机种子也曾随进程变）、`--junitxml` 留计数（`-q` 叠 addopts 的 `-q` 是 `-qq`，汇总行会被吞）。
#    只装 dev 时 integration 里 2 个无环境门的文件会真跑、其余按缺 DSN/URL 自行跳过（这是实测，不是推断）。
cd ../backend && PYTHONHASHSEED=0 uv run pytest -q --junitxml=reports/junit-local.xml

# 4) 前端类型检查与单测
cd ../frontend && npm run typecheck && npm run test

# 5) 起服务：仓库根一条命令同时拉起两端（Ctrl+C 一次，两个进程一起收且不留孤儿——已实测）
cd .. && npm run dev              # API http://127.0.0.1:8000/docs ｜ Web http://localhost:5173

#    要分开跑或换端口（本机 8000 可能被别的东西听过 127.0.0.1，那样后端绑 0.0.0.0:8000 会
#    "看着起来了"而前端全是 404），两个变量必须一起改，它们只有一个真源：
#      cd backend  && AEGIS_HTTP_PORT=8321 uv run python -m aegis.main
#      cd frontend && AEGIS_API_TARGET=http://127.0.0.1:8321 npm run dev

# 6) L3 智能体运行时三态：AEGIS_AGENT_RUNTIME = mock（默认，演练/压测对照）| real | off。
#    real 需配 LLM key，且与 mock 互斥——一个订阅面只挂一副，杜绝双跑抢答。
#    含 LLM 的协同事务走 LLM 档 deadline（AEGIS_SLA_AGENT_TXN_MS，默认 60s），
#    与调度档 ≤2s/≤10s（工作流量测点，禁入 LLM）分档并报表，见《课题6_架构升级方案》§5。
```

跑起来后按这几条自查（都不需要额外工具）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/healthz    # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/readyz     # 200
curl -s http://127.0.0.1:8000/api/v1/integrations | head -c 400           # 每条腿的 driver，见 2.3
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5173/dashboard  # 200（vite 代理到后端）
```

默认这份配置起来时是**纯离线形态**：`store=memory`、`bus=memory`、`retrieval=off`、`knowledge=in_memory`、
`analytics=off`、`tracing=local`——链路照跑、报表照出，但这些腿都还没接上（`/api/v1/integrations`
会把这一件件事如实写出来，别把"200"读成"接上了"）。

### 2.2 路径 B · Docker：后端与依赖进容器（Web 端仍在宿主机）

**先把形态说准**：`deploy/docker-compose.yml` 里**没有前端服务**——容器只编排后端与它依赖的中间件。
Web 端沿用路径 A 的 `npm run dev`：vite 的代理默认就是 `http://127.0.0.1:8000`（`frontend/vite.config.ts`），
而 compose 把后端发布在宿主机的同一个 8000 端口上，所以前端**不需要改任何参数**。
本次实测到的是这条链的两端：容器 healthy 时宿主机 `curl :8000/readyz` 与 `:8000/api/v1/integrations` 都回 200；
"本机 vite 转发到容器后端"这一档我**没有单独取证**（当时 5173 上配对的是本机后端）。
要静态托管就 `npm run build`（实测 51.8s，产出 `frontend/dist` 约 50MB，含 Cesium/底图/地形资产），
但得**自备反向代理**转发 `/api`、`/metrics`、`/healthz`、`/readyz` 四个前缀到后端——本仓不提供站点配置，
`vite preview` 也不带代理（代理只配在 `server` 段），拿它当部署用会一路 404。

profile 分层（本机实测：用模板作 `--env-file`、四条 profile 一起 `config --quiet` 通过；不带 `--env-file` 则当场报 5 个必填项，见 2.3 第 1 条）：

| profile | 起什么 | 谁在用 |
| --- | --- | --- |
| 默认（不带 `--profile`） | `nats`、`postgres`、`neo4j` | 总线 / 关系+空间+向量主存 / 图谱腿 |
| `app` | `backend` | 平台 API 与链路（`env_file: ../.env`，服务名走 compose 网络） |
| `iot` | `emqx` | MQTT 推送腿；默认 `AEGIS_MQTT_ENABLED=false`，不开这条腿 |
| `observability` | `prometheus`、`alertmanager`、`jaeger` | 指标抓取、告警链路、跨度取证（**先起它**，应用才不会把跨度只留在本地）。本轮重写文档时这一档只做到 `config` 解析通过，没有重新起栈取证；起过并量过的记录在 `deploy/observability/README.md` 与 `docs/REPORT.md` |
| `reserved` | `minio`、`redis` | **目前没有消费方**（配置面没有对应键，代码里 0 处 import），所以移出默认档。移出还有一层硬原因：`minio/minio:latest` 已不可拉取（本机实测 `pull access denied`），留在默认档会让每个照文档做的人在 `up` 的第一条命令上失败 |

```bash
# 1) 配置落点：仓库根这份给 compose 插值与容器 env_file；backend/.env 只给本机直接跑的进程
cp .env.example .env
$EDITOR .env        # 上生产前逐个改掉占位口令；要真智能体再填 AEGIS_LLM_API_KEY 并把 AEGIS_AGENT_RUNTIME 改成 real
#    注意 compose 会**整个文件**插值，不只你选中的 profile：实测只删掉 MINIO_ROOT_PASSWORD 一项，
#    哪怕只带 `--profile app` 也会失败（`required variable MINIO_ROOT_PASSWORD is missing a value`）。
#    模板里这四项都给了占位值——POSTGRES_PASSWORD / NEO4J_PASSWORD / MINIO_ROOT_PASSWORD /
#    EMQX_DASHBOARD_PASSWORD——所以照上面 cp 完直接跑就能过解析，但别把占位值留到现场。
#    两份 .env 各有用途，别把实验开关写进 backend/.env——pytest 也会读它（见 2.1 的提醒）。

# 2) 先只解析不启动：这一步会把"必填变量没给"当场暴露出来
docker compose --env-file .env -f deploy/docker-compose.yml --profile app --profile iot --profile observability config --quiet

# 3) 可观测栈先起
docker compose --env-file .env -f deploy/docker-compose.yml --profile observability up -d

# 4) 应用栈：首次会构建镜像，再起容器。本机实测构建耗时——postgres `--no-cache` 冷构建 13.5s
#    （基座镜像已在本地时；全新机器还要先拉 ~892MB 的 postgis 基座），backend 约 13 分钟（688MB，
#    绝大部分时间在下依赖）。起好后用 ps 看 backend 到 healthy 再往下走，别只看 Started。
docker compose --env-file .env -f deploy/docker-compose.yml --profile app up -d
docker compose --env-file .env -f deploy/docker-compose.yml ps        # backend 的 STATUS 要读到 healthy
```

```bash
# 5) 检索权重不烧进镜像（1.1GB）：在能联网的机器上预置到 backend/data/models/，compose 以只读卷挂进 /models
python scripts/fetch_retrieval_models.py
docker compose --env-file .env -f deploy/docker-compose.yml restart backend   # 挂上权重后重启，密集腿才换成真模型

# 6) 图谱腿起来之后把 16 条内置预案灌进去（容器 workdir 就是 /app/backend，脚本随镜像一起装好）。
#    不灌这一句，召回照样有命中，但命中的全是容器里的内存兜底库，图谱那侧一条事实都没有——
#    实测到的表现场原文是链路里的这一行：`案例召回降级: NotFoundError`。
#    逐条落点由命令自己报（exit 3 = 有降级、4 = 写得进去却召回不到），别只看它有没有跑完：
docker compose --env-file .env -f deploy/docker-compose.yml exec backend python -m scripts.ingest_cases
```

起好之后按这几条判读，**不要只看 `/readyz`**（它 200 只说明进程活着）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/readyz
curl -s http://127.0.0.1:8000/api/v1/integrations | python -m json.tool | head -40
docker exec aegis-postgres psql -U aegis -d aegis -tAc \
  "select extname, extversion from pg_extension where extname in ('postgis','vector');"
docker compose --env-file .env -f deploy/docker-compose.yml exec backend python -m scripts.drill --scenario surge
docker compose --env-file .env -f deploy/docker-compose.yml exec backend python -m scripts.metrics_report --rounds 5
```

| 看哪 | 本机实测读回的值 | 读到别的值意味着什么 |
| --- | --- | --- |
| `store` 行 | `driver=postgres`、`connected=true`、`target=postgres:5432/aegis` | 服务名解析不到 ⇒ 依赖容器与后端不在同一网络（2.3 第 2 条） |
| `retrieval` 行 | `driver=bge-m3-int8`、`dense_leg=true`、`degraded=""` | `hashing-ngram-1024` ⇒ 权重没挂上，或被 `.env` 里的相对路径盖掉了 `AEGIS_RETRIEVAL_MODEL_DIR` |
| `knowledge` 行 | `driver=graphiti`（neo4j 在栈里时） | `in_memory` ⇒ 图谱腿没连上（neo4j 未起 / 凭据不符 / 索引初始化超时） |
| `memory` 行 | `driver=postgres` | `memory` ⇒ 共享记忆命名空间所在库没接上（007 迁移没跑到） |
| `delivery` / `tracing` 行 | `mock` / `local` | 这两条默认就不是真的；触达时间与链路取证都属于演练口径 |
| psql 扩展 | `postgis 3.5.2`、`vector 0.8.6` | 报 `extension not available` ⇒ 镜像不是本仓 `deploy/postgres` 构建出来的（2.3 第 5 条） |
| 链路各段 mode | 容器内 `POST /api/v1/reports` 实测：`assess/plan/execute = agent`（53/1903/3313ms），`perceive/feedback = local` | 全 `local` 而 `/api/v1/agents` 又 `online=0` ⇒ 智能体没注册或已停跳（看下一行） |
| `mcp` 行 | 模板默认 `AEGIS_MCP_ENABLED=false` ⇒ `driver=disabled`，此时 `/mcp/` 是 404（这一格读的是模板与装配代码，容器形态本轮未重测） | 改成 true 后这一行应为 `streamable-http`；读到 `absent` ⇒ 镜像里的 extras 少了 `mcp`（`deploy/Dockerfile` 那行 `uv sync` 会整表替换 extras，列漏一个就静默降级） |
| 智能体注册 | `GET /api/v1/agents` 实测回 `online=5`、id 是 `assess.mock01` 等五条 | 后缀就是档位：`.mock01` 是替身、`.real01` 是真内环；`online=0` 而 items 仍有五条 ⇒ 心跳停更但条目在册（见 2.3 第 8 条） |

容器里的智能体档由**仓库根 `.env`** 的 `AEGIS_AGENT_RUNTIME` 决定（模板给的是 `mock`，所以实测那五条是
`*.mock01`）：要跑真智能体就改成 `real`，再
`docker compose --env-file .env -f deploy/docker-compose.yml restart backend`；`real` 与 `mock` 互斥，
一个订阅面只挂一副。注意容器读的是**仓库根那份 `.env`**（`env_file: ../.env`），
改 `backend/.env` 对容器没有任何作用。

停机与清数据（**只有 `-v` 会删数据**，这条差别值得写在这里）：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml --profile app --profile observability down   # 删容器与网络，卷留着（pg_data 等原样保住）
docker compose --env-file .env -f deploy/docker-compose.yml --profile app down -v                          # 连卷一起删＝回到干净环境，重装前确认一遍
```

### 2.3 这几处会咬人（每一条都是本机撞到的）

1. **省掉 `--env-file .env` 就起不来**。compose 的变量插值默认只去 compose 文件所在目录（`deploy/`）找 `.env`，
   而模板在仓库根。实测原文报错：
   `error while interpolating services.postgres.environment.POSTGRES_PASSWORD: required variable POSTGRES_PASSWORD is missing a value`
   ——一次性报 5 个必填项（POSTGRES/NEO4J/MINIO/EMQX/AEGIS_NEO4J），所以每条 compose 命令都得带上这个开关。
2. **`docker run` 起的依赖容器不在 compose 网络上**：backend 里 `nats`/`postgres`/`neo4j` 三个服务名全都解析不到，
   而每条腿仍然各自优雅降级——`/readyz` 照旧 200，于是"什么都没接上"被读成"部署成功"。
   要么整个栈都用这一份 compose 起，要么先把依赖挂进同一网络并带别名：
   `docker network connect --alias nats <compose 网络> aegis-nats`（postgres/neo4j 同理）。
   **反方向的坑也实测到了**：用 `docker compose up --no-deps backend` 去复用外来容器，服务名在启动窗口内可能还没就绪，
   后端会卡在 `nats: no servers available for connection`（`NoServersError`）反复重试、`/healthz` 始终连接被拒——
   别把这条捷径当文档路径。
3. **外来容器的名字会挡 compose 的路**：
   `Error response from daemon: Conflict. The container name "/aegis-neo4j" is already in use by container "4b32e2..."`。
   先 `docker rename aegis-neo4j legacy-neo4j`（或删掉旧容器）再 `up`；改名是安全做法，数据卷不动。
4. **`nats` 的 `-m 8222` 不是可选项**：它的 healthcheck 打的正是 8222，缺了它 backend 的
   `depends_on: service_healthy` 永远不满足。`docker compose config` 查不出这种错（它只看语法）。
5. **postgres 镜像的构建源已经换过位置**：基座 `postgis/postgis:17-3.5` 是 Debian 11(bullseye)，
   而 PGDG 撤掉了 `bullseye-pgdg` 的当前发布目录（`dists/bullseye-pgdg/Release` 现在 404，apt 报
   `does not have a Release file` ⇒ 构建退出码 100）。`deploy/postgres/Dockerfile` 现在把源指到
   `apt-archive.postgresql.org`，**保住的是本仓量过的那一版数据面**（PostgreSQL 17.5 / PostGIS 3.5.2 / pgvector 0.8.6）；
   归档源不再跟安全更新，这是明知故犯的取舍，升级条件与复跑清单写在那个文件的注释里。
6. **权重目录要在 compose 里钉死成挂载点**：`environment`/`env_file` 的优先级高于镜像 `ENV`，
   镜像里的 `AEGIS_RETRIEVAL_MODEL_DIR=/models` 会被 `.env` 的 `./data/models` 盖掉 ⇒ 容器去
   `/app/backend/data/models` 找权重，找不到就把密集腿静默换成词面近似，状态行只剩一个不同的 `driver` 值。
7. **重启以后列表页是空的，不代表数据丢了**。`store` 这条腿的读模型是进程内视图
   （`persistence/postgres.py:271`，`self._read = read_model or PlatformStore()`），启动时不从库里回填。
   实测一次重启后：`GET /api/v1/warnings` 回 `{"count":0,"items":[]}`、按 id 直查回 404，
   而**同一个库**里 `select count(*) from warnings` 是 86。演示/验收前先跑一轮
   `scripts.drill` 把数据写进当前进程，或用直查库的那批出口（`psql`、`/api/v1/geo/*`、
   `scripts.accuracy_replay --from-store`）。这条已经单列为待修缺陷，别把它当部署失败去排查网络。
8. **`/api/v1/agents` 的 `online` 与 `items` 可能各说各话**。本机跑了两小时后读到 `online=0`，
   而 `items` 里 `assess.real01` 等五条仍在册（`healthy=false`、`idle_seconds≈7255`、`served=1`、`failed=1`），
   同时 `gateway_counters.no_agent` 仍是 0——"这副耳朵已经不跳了"与"请求被派给了谁"没对上。
   判读时把 `healthy`/`idle_seconds`/`served` 一起看，别只拿 `online` 当在线证明（已单列待修）。
9. **`uv sync` 的 `--extra` 是整表替换，不是增量**。本机实测：在已装齐 extras 的虚拟环境上跑
   `uv sync --extra dev --extra mcp`，它把没点名的 `postgres/iot/analytics/graph/retrieval` 一并**卸掉**了，
   并在删除被运行中进程占用的 `.pyd` 时中断，留下一个 `import asyncpg` 直接崩的半残环境。
   加一条腿要把已点名的 extras 一起写上（`--extra dev --extra postgres --extra iot ... --extra mcp`），
   改完用 `python -c "import asyncpg"` 这类真导入确认，而不是只看命令退出码 0。
10. **MCP 握手 URL 要带尾斜杠**（`/mcp/`）：`/mcp` 由框架发 307，是否跟随由客户端决定，配错的表现代码是"连上了但没回应"。
    另外这一面读到的数与 REST 同源，也**继承同一个待修缺陷**：新起的进程 `telemetry.query` 回 `count=0`
    （第 7 条那个"读视图不回填"；同一进程、同一参数的 `GET /api/v1/telemetry` 也回 0，而库里 `telemetry_readings` 有 453628 行），
    所以别把工具面的 0 读成"MCP 没接上库"。
11. **配了 `AEGIS_SERVICE_TOKEN` 之后，Web 端会开始吃 401**。鉴权面覆盖的是"取数与审计"这一整层：
    `/api/v1/telemetry`、`/api/v1/stations`、三条 `/geo/*`、`/api/v1/cases/recall`、`/api/v1/retrieval/search`、
    `/api/v1/memory/shared`（读写删）、`/api/v1/audit/chain/{trace_id}`，以及 MCP 工具面——两个面共用同一个令牌、
    认 `X-Aegis-Token` 与 `Authorization: Bearer` 两种头。**前端目前没有带凭据的机制**，所以开令牌的部署里
    地图/数据面板会空（这不是回归，是把"配了令牌"与"真的在拦"对齐的代价）；要么给前端配凭据，要么就别开。
12. **重启后 `GET /api/v1/events` 是空的，但审计端点不是**：前者读进程内视图（第 7 条同源），后者
    `GET /api/v1/audit/chain/{trace_id}` 直读 `chain_runs`。要查"这条预警当时是哪个模型、哪版 prompt 产的"，
    用后者——2026-10-12 实测：重启后事件列表读到 0 条，同一 trace 经审计端点仍 `found=true` 且五段凭证齐全。

### 2.4 出口一览

接口文档 `http://localhost:8000/docs`　指标 `http://localhost:8000/metrics`
装配事实（哪条腿在跑、哪条腿是瘸的）`http://localhost:8000/api/v1/integrations`——`/dashboard` 页底有同源面板
告警链路取证口径 `deploy/observability/README.md`
MCP 工具面（默认关）`http://localhost:8000/mcp/`——**尾斜杠是必需的**（`/mcp` 由框架发 307，是否跟随由客户端决定）；
开启要两步：装可选依赖 `uv sync --extra dev --extra mcp`（extras 整表替换，已有的要列全）+ `AEGIS_MCP_ENABLED=true`。配了 `AEGIS_SERVICE_TOKEN` 时这一面与数据 API
共用同一个令牌，`X-Aegis-Token` 与 `Authorization: Bearer` 两种头都认。
真智能体的佐证取数走哪张门面由 `AEGIS_AGENT_CONTEXT_FACE` 决定（`auto`（默认）/ `rest` / `mcp`，见 `.env.example`）：
`auto` 跟着部署形态走——本进程装得出工具面就走 MCP，否则继续走 REST 兼容面；显式点 `mcp` 而没有工具面时**启动当场失败**，
不静退回 REST。当前生效的那一张写在 `/api/v1/integrations` 的 `mcp` 行 `detail.agent_context_face`。


### Web 页面（纯 Web，响应式，无原生移动端）

| 页面 | 路由 | 内容 |
| --- | --- | --- |
| 态势总览 | `/dashboard` | KPI、风险区域网格、链路各段执行方式（agent/local）、智能体在线状态、SSE 实时事件、页底各条可选腿的三态面板（腿名与后端装配面严格同名，多一条腿没登记标签就会被前端门禁挡住） |
| 一张图 | `/map` | Cesium 三维 + 站点/预警点位；底图 PMTiles、地形自建 quantized-mesh，零 Ion/谷歌依赖；缺瓦片时如实降级为"无底图 + 椭球地形" |
| 监测预警 | `/monitor` | 遥测明细与时序曲线、劣化读数缺口显示、一键发起激增/背景演练；**人工上报入口**（群防群治表单 → 三路解析 → 同一条链路，回显等级/置信/`decided_by`/是否转人工核签与三腿留痕；低置信度由平台自动开出 `human_review` 核签工单，回执带回实例号与待签节点） |
| 预警发布 | `/warnings` | 预警列表与详情、藏汉双语正文（待译显式标注）、通道投递回执、关联任务单元 |
| 流程编排 | `/workflow` | Vue Flow 画布：16 类节点（每一类都有引擎级分派证据，见 `tests/unit/test_workflow_node_dispatch_matrix.py`）、版本化定义与实例运行、人工决策/改参/旁路。配置里点名的 `upstream` 必须是画布上的连线，定义期就校验 |
| 指标量测 | `/metrics` | 运行时埋点的 P50/P95/最大时延 vs 阈值判定、协同成功率、越限项 |
| 智能助手 | `/assistant` | 语义交互：四类任务（查询 / 预案问答 / 演练 / 上报）、意图解构引擎（模糊指令 → 白名单动作链，LLM 只有规划权没有执行权）、流式过程帧、**执行类动作必须人工确认**（越权指令一律拒绝并留痕）；未配 LLM 时页面如实显示"语义服务未配置"而不是装可用 |

## 3. 可选子系统：接得上、退得掉、看得见

平台内核（总线/网关/链路/工作流）不 import 任何可选后端；`integrations.py` 是唯一装配点。
"没装 extra / 连不上后端 / 配置关闭"三种情况都不改变链路语义，只改变状态表里的一行事实——
**"装配时跳过"与"跑起来后降级"必须是两行不同的事实**，否则运维只能靠猜。

| 腿 | 驱动取值 | 关闭 / 降级时的行为 |
| --- | --- | --- |
| `agents` | `mock` \| `real` \| `off` | `real` 需 LLM 网关可用且与 mock 互斥（同一订阅面只挂一副）；智能体缺位/超时/产出不合契约时按段降级规则轨并写入 `degradations`；`off` 时五段全部走平台降级轨 |
| `store` | `memory` \| `postgres` | 落库失败不换实现：内存读视图照常服务，写侧进有界缓冲重试并按计数暴露。**读视图是进程内的、启动时不从库里回填**，所以重启后列表/详情端点看不见已落库的历史（2026-10-12 实测：列表 `count=0`、按 id 直查 404，而同库 `count(*)=86`）——这条不是"降级可见"而是待修缺陷，见 2.3 第 7 条 |
| `analytics` | `off` \| `clickhouse` \| `duckdb` | `off` 时链路里根本不出现 OLAP 代码路径；启用时只 write-behind 入队，热路径永不等 OLAP |
| `knowledge` | `in_memory` \| `graphiti` | 图谱缺位时召回自动落到内置预案库（性质与逐条出处随召回结果一起外显），预案生成照旧完成。写入侧有生产调用点：单条 `POST /api/v1/knowledge/cases` 与整批 `scripts.ingest_cases`，两者响应里的 `driver/degraded/reason` 说清这条案例究竟落在图谱还是只在内存；启动期建一次图谱索引（失败作为 `schema_error`），停服期关一次图谱连接（关不上作为 `close_error`），两件事都出现在这一行 |
| `retrieval` | `off` \| `hybrid` | 权重缺失时稠密腿换确定性词面近似并在 `driver/degraded` 上标出；该分数不得进任何准确率汇报。索引引擎可切 `local`（pgvector + 进程内 BM25）或 `seekdb`（向量 ANN 与 ngram 中文全文同库），切换只改装配，两腿语义与凭证结构不变。**默认是 `local`**：2026-10-02 实测过 seekdb 的几何/约束/LATERAL 能力后，它仍只作为显式 POC 打开，`tests/unit/test_retrieval_wiring.py::TestSeekdbIsNotInTheDefaultShape` 用双向变异把"local 装配去碰 seekdb 代码路径"钉成红 |
| `mqtt` | `off` \| `mqtt` | 推送腿未起不阻断平台；broker 连接状态、读数与溢出计数、`last_error` 全部外显 |
| `weather` | `off` \| `http` | 拉取腿未配 base_url 时完全不存在；启用时按源隔离失败，轮次/条数/失败次数进状态行 |
| `outbound` | `off` \| `http` | 工作流外呼（`api_call` / `device_control` 的出口）：主机白名单为空时整条不接入，节点按"缺少依赖服务"响亮失败；开着时调用/拒发/失败三个计数与 `last_error` 全进状态行，白名单命中的拒发会把这条腿标成降级运行 |
| `tracing` | `local` \| `otlp` | 无 OTLP 端点时跨度只落本地——这一行必须说真话，否则"接了 Jaeger"是假的 |
| `mcp` | `disabled` \| `absent` \| `streamable-http` | 标准 MCP 工具面（§4.2，官方 SDK 的 streamable HTTP）。关着时 `/mcp` 这条路径**不存在**（404），而不是"在但一律 503"；开着但没装可选依赖时路径在、一律 503，且这一行读成 `driver=absent` 并附那条 `--extra mcp` 安装提示——两种"没有工具面"必须能被分辨。装配起来后 `detail` 里同时给 `exposed_tools` 与 `absent_tools`：某条腿没装配（如检索关）时对应工具**不注册**而记入缺席，不留一个"列得到却调不动"的假工具。调用数/失败数/`last_error` 每次读这一行时从运行期台账重取（读数由 `tests/api/test_mcp_api.py::test_腿断了过线报工具错误并把成因带进状态行` 断言：一次失败调用后 `calls={"telemetry.query":1}`、`failures` 同值、成因原样带出）。`detail.agent_context_face` 另说清**研判取数当前走哪张门面**：`mcp` = 真智能体经这张工具面取佐证，`rest` = 走 REST 兼容面，`未起真运行时` = `agent_runtime` 不是 real——"面开着"与"取数切过去了"是两件事，只看前者会读成后者 |

案例入库有两条入口，走的是同一个装配点：单条 `POST /api/v1/knowledge/cases`，整批
`uv run python -m scripts.ingest_cases`（内置 16 条预案模板，或 `--source cases.json` 换外部库）。

```bash
curl -s -X POST http://localhost:8000/api/v1/knowledge/cases \
  -H 'Content-Type: application/json' -d @case.json
# {"case_id":"case_…","driver":"graphiti","degraded":false,"reason":"","title":"…"}

uv run python -m scripts.ingest_cases --dry-run    # 只看要灌哪些条
uv run python -m scripts.ingest_cases              # 整库回填，逐条报落点并回读验证
# {"total":16,"landed":16,"degraded":0,"by_driver":{"graphiti":16},"recall_checked":16,…}
```

两条入口的响应都必须回答"落在哪一侧"，因为图谱写失败时降级链仍会把案例收进进程内库——只看
200 会读成"案例时序知识已更新"，而图其实是空的。批量命令因此还做一次回读（`--no-verify` 可跳过）：
写入返回成功只说明驱动收了字节，能被召回查回来才说明预案真的进了回路；写进去了却召回不到会以
退出码 4 单独报出来。内存形态默认拒绝（`--allow-memory` 才灌，结果里写明 `durable=false`）。
这条写路径刻意不挂进任何自动链路：Graphiti 的一次 `add_episode` ≈ 4—7 次 LLM 调用，塞进预警/研判路径就直接把 ≤3min 指标买掉。

工作流里的 `api_call` / `device_control` 两类节点要向**画布上填写的 URL** 发请求，因此它们由一条
独立的闸管着：`AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS`（逗号分隔的主机白名单，默认空）与
`AEGIS_WORKFLOW_HTTP_TIMEOUT_MS`。留空即整条外呼腿不接入，这两类节点按"缺少依赖服务"响亮失败；
配上白名单后仍只放行 http/https、不跟随重定向、拒绝 URL 内嵌凭据，且异常与状态里只留
`scheme://host`。这条腿本身就是 `GET /api/v1/integrations` 上的一行 `outbound`：`enabled`/`driver`
与白名单（已收敛成主机名，凭据进不来）、`calls`/`rejected`/`failures` 三个计数、`last_error`
全都在里面；白名单命中（画布上填了不放行的主机）会把它标成"降级运行"，因为那正是
"节点为什么一直失败"的第一现场。

凭据一律不出现在状态接口与日志里：DSN/URI/端点都先脱敏（只留 `scheme://host[:port]/path`）再出口，
客户端与导出器仍拿到完整值。

## 4. 一键演练

```bash
cd backend
uv run python -m scripts.drill --scenario surge     # 注入激增监测数据并跑通全链路
uv run python -m scripts.metrics_report             # 打印时延/成功率实测报告（对齐考核指标）
uv run python -m scripts.zenoh_poc                  # 站端↔网关弱网链路 POC（需要 zenoh 与本机端口）
uv run python -m scripts.load_curve --levels 1,10,30,60 --duration 25s
                                                    # 并发曲线：自己起服务、按梯度跑 Locust、报出拐点
uv run python -m scripts.emit_model_cards          # 各智能体模型卡片（模型/Prompt 版本/评测分；未评测如实 None）
uv run python -m scripts.quality_gate --baseline baselines/quality_baseline.json
                                                    # 质量表回归门禁：任一指标较基线回退即红。
                                                    # `--baseline` 不能省：不带它，基线缺席＝无判定（CI 那句带着它）
                                                    # 首过率还要真调用台账：加 --usage <gateway-usage.json>，无台账＝该列 None
AEGIS_PG_DSN=postgresql://<user>:<password>@127.0.0.1:5432/<db> \
uv run python -m scripts.load_curve --profile deployed --levels 1,10,30,60 --duration 25s
                                                    # 同一套曲线换到真 NATS JetStream + 真 PostgreSQL 上量。
                                                    # DSN 必须显式给：`.env` 里 `AEGIS_PG_DSN=` 是故意留空的
                                                    # 模板，缺它脚本直接判失败，不会静默退回内存视图。
                                                    # 服务子进程的 stdout/stderr 落在 reports/load_curve_server.log，
                                                    # 起不来时报错会直接把日志尾巴一起给出
uv run python -m scripts.metrics_report --dataset labels.jsonl
                                                    # 附现场标注案例集才算得出"预警准确率"；不附则如实写"未测得"
AEGIS_PG_DSN=postgresql://<user>:<password>@127.0.0.1:5432/<db> \
uv run python -m scripts.accuracy_replay --import-labels labels.jsonl
                                                    # 把现场真值标注导入 warning_truth_labels（按 case_id 幂等）
uv run python -m scripts.accuracy_replay --from-store --since 2026-09-01T00:00:00Z --kind field
                                                    # 库侧回放：真值来自标注表，预测来自落库 warnings
                                                    # （时间必须带时区；标注没声明是现场数据时官方准确率恒为"未测得"）
curl -s 'http://localhost:8000/api/v1/accuracy/replay?since=2026-09-01T00:00:00Z&kind=field&min_field_cases=20'
                                                    # 同一份报表的 HTTP 出口（大屏读这里）。判定只在
                                                    # persistence/replay.py 一处；store_backend=memory 时这条回 503 并说明要配 postgres
```

## 5. 关键设计约束

| 约束 | 说明 |
| --- | --- |
| 契约先行 | 智能体只经 NATS 总线与 `AgentMessage v1` 交互，不共享代码、不直连数据库 |
| 智能体优先、平台降级 | 每段链路在智能体缺位/超时/产出不合契约时自动回退规则引擎与本地剧本，链路不中断 |
| LLM 五条安全性质 | LLM 可进关键路径，但每步**可降级**（deadline 到即走规则轨）、**可审计**（各段的模型与 Prompt 版本由智能体自己声明、链路只转记，落在 `chain_runs.stages[].model_info`；`GET /api/v1/audit/chain/{trace_id}` 直读库，重启后仍能查——本地降级腿【不带】这个键，缺席就是"这一程没有模型参与"，不是"走了没记名"）、**可解释**（`rationale`+`evidence_refs` 必填）、**限时**（LLM 档 `sla_agent_txn_ms`，与调度档 ≤2s/≤10s 分档并报表）、**有栅栏**（动作白名单、等级枚举冻结、数字防幻觉闸） |
| 发送权归属平台 | execute 智能体只产受众差异化文案；真实触达只经平台交付腿（DeliveryDispatcher），回执证据只认交付腿 receipt——智能体自报送达不采信（铁律 7：指标必须实测） |
| 外部文本必过围栏 | 上报/案例/检索片段/回执进 prompt 前包进不可伪造的分隔围栏（提示注入防线）：内容原样保留，但只是数据、不再是指令 |
| 指标必须实测 | `latency_report()` 输出运行时埋点的分位数与 SLA 判定，验收数字全部可追溯到样本 |
| 降级留痕 | 降级写入 `ChainResult.degradations`（可归因），致命错误写入 `errors`（判失败） |
| 双语不伪造 | 未接入可信翻译能力时预警仅中文并标记 `translation_pending`，不生成未经审核的藏语文本 |
| 数据不编造 | 站点坐标、案例出处等缺失时如实返回 `null`／标注"自编预案模板"，绝不用看起来合理的值填空 |
| 配置面无空旋钮 | `.env.example` 与 compose 里写下的每个 `AEGIS_*` 都必须被生产代码读到；暂未接线的必须响亮拒绝（`NotImplementedError`），不得静默走默认 |
| 单源失败要隔离 | 摄取按源独立超时与失败记账：一个站端断了不拖垮整轮采集（高原弱网是常态） |

## 6. 开发规范

- 提交遵循 Conventional Commits（`feat|fix|chore|test|docs|perf|refactor(scope): 描述`），不带 AI 生成尾注
- 门禁：`ruff format` + `ruff check` + `mypy` + `pytest` 全绿方可合并（见 `.github/workflows/ci.yml`）
- 契约变更：语义化版本 + 更新一致性测试套件 + 记录 ADR
- 测试金字塔：单元测试（含边界/属性测试 hypothesis）→ 契约与模糊（schemathesis）→ 集成（真服务端，按环境变量开关）→ 压测（Locust）

## 7. 许可

AGPL-3.0（与基线项目 NexusMind 保持一致）。第三方组件许可证与版本逐条记录在
`docs/技术选型与许可证清单.md`（按安装产物实测，不凭印象填写）。
