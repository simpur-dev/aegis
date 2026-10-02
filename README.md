# AEGIS

**Adaptive Emergency Geo-hazard Intelligence System**
西藏山地灾害多智能体协同调控技术与平台 —— 平台侧（后端 + Web）

面向西藏高原 5 类典型山地灾害（滑坡、崩塌危岩、泥石流、冰雪雪崩、冰湖溃决），
构建"感知—研判—决策—执行—反馈"全链条协同调控平台。

- 应用形态：**纯 Web 平台**（响应式，不含原生移动端）
- 分工边界：**平台侧**（L1 接入 / L2 数据 / L4 服务 / L5 应用 / 基座）由本仓库承载；
  **L3 五大智能体**由智能体方按 `contracts/AGENT_INTEGRATION_SPEC.v1.md` 接入
- 架构依据：《课题6_项目架构设计》五层+一基座，融合"一张图 + 一总线 + 四预"扩展
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
│   │   ├── services/          # 触发规则、风险定级、任务拆解、预警生成、靶向触达、LLM 网关
│   │   ├── workflow/          # 自研 DAG 引擎（16 类节点）与内置模板
│   │   ├── edge/              # 站端↔网关弱网链路（Eclipse Zenoh POC：有界缓冲 + 按序重放）
│   │   ├── observability/     # 时延账本、OpenTelemetry 埋点与导出、Prometheus 导出
│   │   ├── agents/            # 参考智能体（开发与门禁用）
│   │   └── api/               # FastAPI HTTP/SSE 接口与工作流接口
│   ├── scripts/               # drill / metrics_report / accuracy_replay / load_curve / zenoh_poc
│   ├── tests/                 # unit / contract / integration / e2e / api / perf / load
│   └── pyproject.toml         # extras：postgres / iot / graph / retrieval / analytics / edge / dev
├── frontend/                  # Vue 3 + Vite；Cesium 一张图、Vue Flow 编排画布
├── deploy/                    # docker-compose（含 profile）、镜像构建、Prometheus/Alertmanager 配置
├── docs/                      # 架构说明、ADR、实测记录（REPORT.md）、选型与许可证清单
└── scripts/                   # 检索权重预置脚本（内网/离线部署前跑一次）
```

## 2. 快速开始

```bash
# 1) 配置：LLM 与图谱项可留空，平台按规则引擎与内存案例库降级运行
#    注意：`Settings` 的 env_file 按进程工作目录解析，`backend/.env` 会被 pytest 一起读走。
#    本机实验请用一次性环境变量，别写进 .env——否则"全绿"量到的是你个人的机器偏好：
#      AEGIS_RETRIEVAL_ENABLED=true AEGIS_RETRIEVAL_INDEX_BACKEND=seekdb uv run python -m aegis.main
cp .env.example backend/.env

# 2) 依赖（后端 uv / 前端 npm）
cd backend && uv sync --extra dev
cd ../frontend && npm install

# 3) 全量测试（离线可跑：总线用内存实现，可选腿一律关闭）
cd ../backend && uv run pytest -q

# 4) 前端类型检查与单测
cd ../frontend && npm run typecheck && npm run test

# 5) 起服务
cd ../backend && uv run python -m aegis.main      # API: http://127.0.0.1:8000/docs
cd ../frontend && npm run dev                     # Web: http://localhost:5173（代理 /api 到后端）
```

需要真实基础设施时按 profile 起（`observability` 先起，应用才不会把跨度只留在本地）：

```bash
# compose 的变量插值默认只在 compose 文件所在目录找 .env，
# 而凭据模板在仓库根：不显式指过去，`up` 会在 POSTGRES_PASSWORD 这类必填项上直接失败。
cp .env.example .env
docker compose --env-file .env -f deploy/docker-compose.yml --profile observability up -d
docker compose --env-file .env -f deploy/docker-compose.yml --profile app --profile iot up -d
# 两份 .env 各有用途：仓库根这份给 compose 插值与容器 env_file，backend/.env 给本机直接跑进程；
# 别把实验开关写进 backend/.env——pytest 也会读它（见上面"快速开始"里的提醒）。
# 混合检索需要权重，先在能联网的机器上预置到 backend/data/models/（约 1.1GB，只读挂载进容器）
python scripts/fetch_retrieval_models.py
```

接口文档 `http://localhost:8000/docs`　指标 `http://localhost:8000/metrics`
装配事实（哪条腿在跑、哪条腿是瘸的）`http://localhost:8000/api/v1/integrations`——`/dashboard` 页底有同源面板
告警链路取证口径 `deploy/observability/README.md`

### Web 页面（纯 Web，响应式，无原生移动端）

| 页面 | 路由 | 内容 |
| --- | --- | --- |
| 态势总览 | `/dashboard` | KPI、风险区域网格、链路各段执行方式（agent/local）、智能体在线状态、SSE 实时事件、页底七条可选腿的三态面板 |
| 一张图 | `/map` | Cesium 三维 + 站点/预警点位；底图 PMTiles、地形自建 quantized-mesh，零 Ion/谷歌依赖；缺瓦片时如实降级为"无底图 + 椭球地形" |
| 监测预警 | `/monitor` | 遥测明细与时序曲线、劣化读数缺口显示、一键发起激增/背景演练 |
| 预警发布 | `/warnings` | 预警列表与详情、藏汉双语正文（待译显式标注）、通道投递回执、关联任务单元 |
| 流程编排 | `/workflow` | Vue Flow 画布：16 类节点、版本化定义与实例运行、人工决策/改参/旁路 |
| 指标量测 | `/metrics` | 运行时埋点的 P50/P95/最大时延 vs 阈值判定、协同成功率、越限项 |

## 3. 可选子系统：接得上、退得掉、看得见

平台内核（总线/网关/链路/工作流）不 import 任何可选后端；`integrations.py` 是唯一装配点。
"没装 extra / 连不上后端 / 配置关闭"三种情况都不改变链路语义，只改变状态表里的一行事实——
**"装配时跳过"与"跑起来后降级"必须是两行不同的事实**，否则运维只能靠猜。

| 腿 | 驱动取值 | 关闭 / 降级时的行为 |
| --- | --- | --- |
| `store` | `memory` \| `postgres` | 落库失败不换实现：内存读视图照常服务，写侧进有界缓冲重试并按计数暴露 |
| `analytics` | `off` \| `clickhouse` \| `duckdb` | `off` 时链路里根本不出现 OLAP 代码路径；启用时只 write-behind 入队，热路径永不等 OLAP |
| `knowledge` | `in_memory` \| `graphiti` | 图谱缺位时召回自动落到内置预案库（性质与逐条出处随召回结果一起外显），预案生成照旧完成。写入侧有生产调用点：`POST /api/v1/knowledge/cases` 是唯一入口，响应里的 `driver/degraded/reason` 说清这条案例究竟落在图谱还是只在内存；启动期还会建一次图谱索引，失败作为 `schema_error` 出现在这一行 |
| `retrieval` | `off` \| `hybrid` | 权重缺失时稠密腿换确定性词面近似并在 `driver/degraded` 上标出；该分数不得进任何准确率汇报。索引引擎可切 `local`（pgvector + 进程内 BM25）或 `seekdb`（向量 ANN 与 ngram 中文全文同库），切换只改装配，两腿语义与凭证结构不变。**默认是 `local`**：2026-10-02 实测过 seekdb 的几何/约束/LATERAL 能力后，它仍只作为显式 POC 打开，`tests/unit/test_retrieval_wiring.py::TestSeekdbIsNotInTheDefaultShape` 用双向变异把"local 装配去碰 seekdb 代码路径"钉成红 |
| `mqtt` | `off` \| `mqtt` | 推送腿未起不阻断平台；broker 连接状态、读数与溢出计数、`last_error` 全部外显 |
| `weather` | `off` \| `http` | 拉取腿未配 base_url 时完全不存在；启用时按源隔离失败，轮次/条数/失败次数进状态行 |
| `outbound` | `off` \| `http` | 工作流外呼（`api_call` / `device_control` 的出口）：主机白名单为空时整条不接入，节点按"缺少依赖服务"响亮失败；开着时调用/拒发/失败三个计数与 `last_error` 全进状态行，白名单命中的拒发会把这条腿标成降级运行 |
| `tracing` | `local` \| `otlp` | 无 OTLP 端点时跨度只落本地——这一行必须说真话，否则"接了 Jaeger"是假的 |

案例入库走 `POST /api/v1/knowledge/cases`（知识层唯一的写入口）：

```bash
curl -s -X POST http://localhost:8000/api/v1/knowledge/cases \
  -H 'Content-Type: application/json' -d @case.json
# {"case_id":"case_…","driver":"graphiti","degraded":false,"reason":"","title":"…"}
```

响应必须回答"落在哪一侧"，因为图谱写失败时降级链仍会把案例收进进程内库——只看 200 会读成
"案例时序知识已更新"，而图其实是空的。这条写路径刻意不挂进任何自动链路：Graphiti 的一次
`add_episode` ≈ 4—7 次 LLM 调用，塞进预警/研判路径就直接把 ≤3min 指标买掉。

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
```

## 5. 关键设计约束

| 约束 | 说明 |
| --- | --- |
| 契约先行 | 智能体只经 NATS 总线与 `AgentMessage v1` 交互，不共享代码、不直连数据库 |
| 智能体优先、平台降级 | 每段链路在智能体缺位/超时/产出不合契约时自动回退规则引擎与本地剧本，链路不中断 |
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
