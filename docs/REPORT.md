# REPORT：实测记录与证据链

这份文件只收**由真实产物量出来的数字**（命令、口径、样本量都写清楚），以及明确还没测到的东西。
代码注释里的"见 REPORT"指向这里。任何数字过期就删掉重测，不做估算。

## 环境与口径

- 日期：2026-10-01；机器：单台 x64 Windows，CPU only（无 GPU、无独立推理卡）。
- 存储后端：`store_backend=memory`（除特别说明外）；总线 `memory`；交付通道 `mock`。
  触达时延里的 1.2s 是 mock 通道适配器注入的模拟链路耗时，不是真实短信/北斗时延。
- 测试规模（本机 `pytest --collect-only`）：收集用例 **1628** 条（含参数化与 hypothesis 展开的样例计数），
  分布在 57 个测试文件；`src/` 85 个模块、18461 行。
- 门禁：`ruff format --check`、`ruff check`、`mypy src/aegis`、全量 `pytest` 四项必须全绿才算完成。

## 考核指标实测

### 降级模式（平台独立完成，无智能体）

`python -m scripts.metrics_report --rounds 10`（seed 202609，10 轮，29 个事件，致命错误 0）

| 指标 | 阈值 | 实测 | 判定 |
| --- | --- | --- | --- |
| 常规任务调度响应 ≤2s | 2000ms | P50 0.055 / P95 0.081 / 最大 0.088ms | 达标（越限 0） |
| 预警信息生成 ≤3min | 180000ms | P50 0.0 / P95 0.0 / 最大 1.0ms | 达标（越限 0） |
| 预警信息靶向触达 ≤20min | 1200000ms | P50 1204.5 / P95 1230.4ms | 达标（越限 0） |
| 多节点数据共享同步 ≤3s | 3000ms | 样本 0 | 该模式不跑工作流，未测得 |
| 异常工况识别与重调度 ≤10s | 10000ms | 样本 0 | 同上 |

### 智能体在场模式

`python -m scripts.metrics_report --agents --rounds 6`（seed 202609，17 个事件，51 个事务样本）

| 指标 | 阈值 | 实测 | 判定 |
| --- | --- | --- | --- |
| 多节点数据共享同步 ≤3s | 3000ms | P50 3.04 / P95 6.00 / 最大 7.63ms | 达标（越限 0） |
| 异常工况识别与重调度 ≤10s | 10000ms | P50 28.9 / P95 36.5 / 最大 37.6ms | 达标（越限 0） |
| 常规任务调度响应 ≤2s | 2000ms | P50 30.5 / P95 40.2ms | 达标（越限 0） |
| 预警信息生成 ≤3min | 180000ms | P50/P95/最大 0.0ms | 达标，但见下方"口径提醒" |
| 预警信息靶向触达 ≤20min | 1200000ms | P50/P95/最大 0.0ms | 达标，但见下方"口径提醒" |
| 多智能体协同联动成功率 ≥90% | 90% | 100%（51 个事务） | 达标 |

**口径提醒（不要把这些 0.0 读成"零耗时"）**：

- `warning_generation_ms` 量的是"定级结论 → 预警记录构造"这一段纯内存动作，本身就在亚毫秒级，
  端到端的预警耗时看链路总时延而不是这一项。
- `--agents` 下 `warning_reach_ms` 记为 0.0，是因为投递由执行智能体完成后回执给平台，
  平台侧不再走自己的通道埋点。两种模式量到的不是同一条路径，横向比较要用同一模式。

## 检索与知识层实测（bge-m3 / bge-reranker int8，CPU）

权重：`backend/data/models/bge-m3-int8`（559MB）与 `bge-reranker-v2-m3-int8`（561MB），
由 `scripts/fetch_retrieval_models.py` 预置并带 sha256 清单。

| 量 | 结果 | 说明 |
| --- | --- | --- |
| 会话装载 | 冷启动 embed 2102ms、rerank 4732ms | 已挪到容器启动期（`warm_retrieval`），不再由第一条预警付 |
| 查询侧嵌入（单条短文本） | 热态中位 26.5ms | 密集腿每请求只嵌一条查询 |
| 文档侧嵌入（5 条预案正文，非合成短句） | 热态中位 3807ms | 属于建索引路径，不在预警请求路径上 |
| 交叉编码重排 5 对（真实正文 ~305 字） | 热态中位 4201ms | 决定预算的主要成本项 |
| 交叉编码重排 10 对 | 热态中位 8361ms | 因此 `rerank_top_n=5`：多排的候选会被 k 截断丢掉 |
| 词法腿（BM25，16 条案例语料） | 热态中位 ~1ms | 零外部依赖，权重缺失时仍能出结果 |
| 端到端一条链路（含检索 + 重排） | 单区域 4.3—4.5s | 占 ≤3min 预警口径的 2.4%，无降级记录 |

据此定的默认值：`retrieval_budget_ms=5000`、`_RERANK_TOP_N=5`，两者是一组配套数字，
改一个必须重测另一个（`tests/unit/test_retrieval_wiring.py::test_default_budget_covers_a_measured_rerank_round`）。

**准确率口径的红线**：权重缺失时密集腿会切到 `HashingEmbedder`（确定性词面近似，无跨词面泛化能力）。
该降级必须在 `GET /api/v1/integrations` 的 `driver`/`degraded` 上看得见，其分数不得进入任何
"预警准确率 ≥90%" 类汇报。

## 工作流引擎实测

容器启动后 `WorkflowEngine.node_types` 返回 **16** 类节点（≥10 的指标要求），
时延见上表"异常工况识别与重调度""常规任务调度响应"两行；证据与对照分析在
`docs/adr/0004-self-built-dag-workflow.md`。

## 在线取证（2026-10-01，Docker 真实服务端）

| 取证对象 | 服务端实测版本 | 命令 | 结果 |
| --- | --- | --- | --- |
| PostgreSQL + PostGIS + pgvector | PostgreSQL 17.5 / PostGIS 3.5.2 / pgvector 0.8.6（`deploy/postgres` 镜像） | `AEGIS_TEST_PG_DSN=... pytest tests/integration/test_persistence_postgres.py` | 19 项全通过：扩展 DDL、`ST_DWithin` 按米、多边形覆盖、pgvector 余弦排序、写缓冲在库不可达时保住热路径 |
| ClickHouse 分钟物化 | ClickHouse 26.9.5.2 | `AEGIS_TEST_CLICKHOUSE_HOST=... pytest tests/integration/test_analytics_clickhouse_live.py` | 8 项全通过，并**因此发现并修掉两个真缺陷**：MV 的 SELECT 缺 `AS` 别名（THERE_IS_NO_COLUMN）、聚合列类型没跟服务端推断的 `Nullable` 状态类型对齐（CANNOT_CONVERT_TYPE）。服务端聚合与 Python 参考实现 `materialize_minutes` 逐键对账一致 |
| Jaeger 链路找回 | all-in-one（镜像摘要 `ab6f1a1f…`） | `AEGIS_TEST_OTEL_ENDPOINT=http://127.0.0.1:4318 pytest -m slow tests/integration/test_telemetry_otel.py`；另跑一条真实灾害链路 | 3 项通过；真实链路以 `hazard_chain` 为根导出 6 个跨度（根 + perceive/assess/plan/execute/feedback），可用 `/api/traces/<traceID>` 找回，`aegis.*` 属性（region/readings/risk_level/stages/ok/degradations）在 UI 侧可读 |
| 告警规则与采集配置 | promtool（prom/prometheus v2.55.0） | `docker run --entrypoint /bin/promtool ... check rules deploy/observability/alerts.yml` | `SUCCESS: 9 rules found`；`check config deploy/prometheus.yml` 亦通过 |
| 告警链路端到端（规则求值→分发→抑制） | prom/prometheus v2.55.0（摘要 `378f4e03…`）、prom/alertmanager v0.27.0（摘要 `e13b6ed5…`） | `promtool test rules deploy/observability/alerts-test.yml`；`amtool check-config alertmanager.yml`；`AEGIS_TEST_ALERTMANAGER_URL=… AEGIS_TEST_PROMETHEUS_URL=… pytest tests/integration/test_alerting_live.py` | 9 条阈值的 promtool 单元用例 `SUCCESS`（每条告警一对"越限要报/健康不误报"）；amtool `SUCCESS`（route + 1 inhibit rule + 2 receivers）；真服务上 3 项通过：Prometheus 加载全部 9 条规则且 `health=ok`、把 `alertmanager:9093` 发现为活动分发目标、critical 路由到 `duty-escalation` 而 warning 落到 `platform-oncall`，同实例同团队的 warning 被抑制为 `suppressed` |
| 站点清单 HTTP 面 | 同上（PostgreSQL 17.5 + PostGIS 3.5.2），进程内 ASGI 直连真库 | `GET /api/v1/stations`、`GET /api/v1/stations?region_code=540221`、`GET /api/v1/stations?region_code=5401` | 返回两条真实记录：维表登记的 `RG-540121-01` 带站名/高程 3650m/`SRID=4326;POINT(91.28 29.896)`；只报过数未登记的 `NEW-540221-77` 坐标为 `null`（**不编造坐标**，前端据此列入"未定位"）。区划过滤生效，非法区划码 422 |
| Zenoh 站端↔网关链路（P1） | eclipse-zenoh 1.10.1（本机 Python 运行时，两个 peer 会话经 tcp/127.0.0.1 直连） | `pytest tests/unit/test_edge_zenoh_transport.py`；`AEGIS_TEST_ZENOH=1 pytest tests/integration/test_edge_zenoh_live.py` | 新增 44 项替身驱动单测 + 4 项真运行时在线用例，全部通过；**因此发现并修掉三个真缺陷**：① `build_config()` 写了本版本不存在的 `scouting/multicast/join_interval`、`join_messages`，且 `interface` 被当数组传（应为字符串），而 `from_env()` 默认就开组播——也就是环境装配路径一 connect 就抛 `unknown key`；② 边缘缓冲的 store/query 原本"逐条 reply + 收尾 done 标记"，本版本 queryable 对同一 ke 连发多条时请求侧只收到其中一条，实测改成"一次查询一份完整应答"；③ 通配查询无法构造应答 ke 时原先静默不回，让对端干等到超时，现在回 `reply_err` 说明原因 |
| NATS JetStream 平台总线（P1：总线仍用 NATS） | nats:2.10-alpine（本机容器 `-js -sd`）+ nats-py（核心依赖） | `AEGIS_TEST_NATS_URL=nats://127.0.0.1:4222 pytest -q -m nats tests/integration/test_nats_bus.py`（连跑三轮全绿）；`pytest tests/unit/test_nats_api_contract.py`（21 项） | **这条 P1 此前从未在真服务器上跑通过**，接上以后一次暴露四个问题：① `error_cb` 是同步方法，而 nats-py 在 `connect()` 里就校验回调必须是协程 → 真实总线连不上（`InvalidCallbackTypeError`）；② `duplicate_window` 按纳秒传，库内又乘一次 1e9 → 1.2e20 溢出 int64，服务端对建流回 `invalid JSON`；更要命的是 `connect()` 把建流异常咽在 `log.debug` 里并照旧 `_connected=True`，平台于是带着一条"每条发布都失败"的已连接总线继续跑——现在改为抛 `BusNotReadyError` 并关掉已建立的连接，装配面看得见；③ `js.subscribe(queue=..., durable=...)` 两者不等会被库拒绝 → 网关的持久上行订阅一条都建不起来，队列组名改为沿用消费者名；④ 用例侧两处只在真服务器上才成立的问题：每条用例换流前缀却不删流（NATS 2.10 拒绝主题重叠建新流，err_code 10065），以及一条用例断言的 `received` 列表根本没有订阅方往里写。CI 的 `integration-nats` job 原来用 `services:` 起 NATS：既没开 `-js`，健康检查又打向未启用的 8222 监控端口——这条 job 在此之前不可能变绿，已改成显式 `docker run ... -js -m 8222` 并以 `/jsz` 的 `enabled:true` 做就绪自检 |
| MQTT 推送腿（站端→平台） | aiomqtt 2.5.1 + paho-mqtt 2.1.0（`[iot]` extra），eclipse-mosquitto 2（broker，本机线级验证；生产按方案用 EMQX 5.8） | `pytest tests/unit/test_mqtt_connector.py tests/unit/test_mqtt_wiring.py`；`AEGIS_TEST_MQTT_HOST=127.0.0.1 pytest tests/integration/test_mqtt_live.py` | 新增 43 项单测 + 3 项真 broker 用例全部通过：三条 topic 解出四条读数（含批量与单条两种形状、多级子路径），坏载荷按原因计数而不是静默丢弃，broker 端口不通时 `connected=false` 且退避重连有计数与 `last_error`；缓冲有界（满则丢最旧并计数），凭据不进状态面。**过程中的真实约束**：paho 的 asyncio 支持依赖 `loop.add_reader/add_writer`，Windows 默认 Proactor 循环没有这两个钩子（Linux 容器部署不受影响），已把这条写进模块头并把错误翻译成可操作提示 |
| 内置案例库的性质披露 | 无外部服务（`backend/src/aegis/knowledge/data/hazard_cases.json` 本体） | `pytest tests/unit/test_knowledge_dataset_provenance.py` | 17 项通过：16 条逐条带可核对出处（最短 48 字，全部指向 NexusMind 骨架移植或平台自编兜底）、`plan_brief()` 不漏一条出处、图谱侧无本体时不编造出处、`observed_at` 不落在未来、`confidence` 有区分度（15 个不同值/16 条）、`/api/v1/integrations` 与 `/api/v1/cases/recall` 两端同时披露库性质。**修掉两处**：① 配置注释与装配文档原写"16 条西藏案例"，读起来像从真实事件里清洗出的经验库，实际是自编预案模板；② `.gitignore` 的 `data/` 通配把 `backend/src/aegis/knowledge/data/` 一起吞了——内置案例库**从未入库**，新克隆的知识层降级没有兜底数据（本地测试因为文件在盘上照样全绿），已显式反排除并加一条 `git check-ignore` 守卫用例 |
| 公开气象拉取腿（第三条接入腿） | httpx 真客户端 + `httpx.MockTransport`（形状约定见连接器头注释） | `pytest tests/unit/test_weather_connector.py tests/unit/test_ingest_isolation.py` | 新增 19 + 6 项全部通过：12 个观测量按单位真源出读数、null/非数字/缺站名/区划过短逐条跳过、负值除气温外判 `suspect`、区划大写并截 24、`observed_at` 归一到 `Z`；形状不符（缺 `stations`、响应不是对象）抛 `SchemaInvalidError` 而不是当成"本轮无数据"；HTTP 5xx 与连接失败按源隔离，错误摘要里的请求 URL 整段换成 `<endpoint>` 占位（凭据常在运营方填的 base_url 里）；注入的客户端不被连接器关闭，自建的在 `shutdown` 时关闭。**真 socket 上另跑了一轮**（`tests/integration/test_weather_transport_live.py`，用标准库 `asyncio.start_server` 起本机 HTTP 服务，常驻回归）：对端记录到 `GET /observation HTTP/1.1`、两轮共用同一连接池（`rounds=2/readings=6`）、容器一轮摄取 3 条入库且状态行如实报 `driver=http`；对端只写响应头就断开时，`httpx.HTTPError` 上抛并计一次失败、错误摘要里只剩 `<endpoint>`。顺带确认一处口径：拉取腿的接入时延按"上游观测时刻 → 入库可查时刻"计，上游给一条旧读数就会判"接入时延超 SLA"——这是设计意图，数据新鲜度本来就是 ≤5min 接入指标的一部分。**修掉一个结构性的真缺陷**：`IngestService` 用 `asyncio.gather(..., return_exceptions=False)` 采集，第一个异常会被直接抛出 `ingest_once`，下面那段"按源写 `sources_failed`"的分支永远走不到——模块头部承诺的"单源失败隔离（高原弱网常见）"只存在于注释里；改为 `return_exceptions=True` 并补 `tests/unit/test_ingest_isolation.py`（此前没有任何测试构造过"有好有坏"的源组合） |
| 预警准确率回放落到报表出口 | 本机 CLI（`scripts.metrics_report --dataset`），三组合成/现场结构的 JSONL 数据集 | `python -m scripts.metrics_report --rounds 2 --dataset <f.jsonl>`，另跑 `pytest tests/unit/test_accuracy_replay_report.py` | 判据分支全部在真命令上跑过：合成集 24 例 → `status=not_measured`、`indicator=synthetic_only`、`official_accuracy=None`、实测算式 20/24=0.8333 仍如实给出、退出码 0；现场结构 45 例（TP10/FP20/FN5/TN10）→ `measured`+`not_met`、`official_accuracy=0.4444`、判定"未达标"、退出码 1（CI 可据此拦）；不给 `--dataset` → 准确率不进"指标判定"，只留在"未覆盖指标"并写明用哪个开关补。此前 `persistence/replay.py` 的口径早已定死但**没有任何生产入口**，准确率只能停在"未测得"。同一轮把演练/报表脚本里 `container.transport.idle()` 的隐性假设收进 `bus.transport.drain_pending()`：只有内存总线有"本地在途队列"可排，换 NATS 时不该撞 AttributeError |
| 配置面旋钮可达性（静态对账） | 无外部服务（`Settings` 字段 ↔ 生产源码 ↔ `.env.example`） | `pytest tests/unit/test_config_knob_reachability.py` | 9 项通过并**抓到三个拧不动的旋钮**：`simulator_enabled`（`.env.example` 明写 `AEGIS_SIMULATOR_ENABLED`，容器却只看 `with_simulator` 形参）、`weather_api_base_url`（连接器写了但装配层从不构造）、`connector_poll_seconds`（与 `simulator_interval_seconds` 同义重复，两个旋钮管同一件事注定有一个是假的）；另有两个纯元数据字段 `app_name`/`hazard_scope` 删除（灾种口径的唯一真源是 `domain/enums`，配置里再抄一份必然漂移）。守卫口径：`Settings` 每个字段都要在生产源码里被读到，`.env.example` 里每个 `AEGIS_*` 都要落到被读的字段；确实暂未接线的必须进 `RESERVED` 清单**且代码路径响亮拒绝**（`delivery_mode=http` 抛 `NotImplementedError`，不静默回落 mock 通道） |

同一轮还纠正了一处装配缺陷：`telemetry.init_telemetry()` 过去只有测试在调用，生产进程从未装配 provider，
导致"接了 OpenTelemetry + Jaeger"实际是跨度全留本地。现由应用生命周期负责装配/关停，
并在 `GET /api/v1/integrations` 增加 `tracing` 一行（`otlp`/`local` + 端点 + 丢弃跨度数）；
端点在这一行与启动日志里统一只留 `scheme://host:port/path`，采集器凭据不进任何对外面（导出器仍拿完整值）。

另一处同类缺陷是**告警链路本身**：`deploy/observability/alerts.yml` 早就写好并单独过 promtool 校验，
但 `deploy/prometheus.yml` 里既没有 `rule_files` 也没有 `alerting` 段——Prometheus 于是只抓样本、
从不求值那九条 SLA 规则，Alertmanager 更是一个告警也收不到。"规则文件存在"与"告警链路在跑"是两件事，
现已补齐挂载与 alertmanager 服务，并由 `tests/unit/test_alerting_topology.py`（静态四向对账：
`rule_files` 路径必须真被 compose 挂进容器、规则里的 `aegis_*` 必须真出现在 `/metrics`、
路由分组键与抑制对齐键必须在每条告警 labels 上取得到值）与上面那条在线用例一起钉住。
取证环境与四道校验的操作口径写在 `deploy/observability/README.md`。

## 并发曲线实测（2026-10-01，Locust）

命令：`uv run python -m scripts.load_curve --levels 1,10,30,60 --duration 25s`
与 `uv run python -m scripts.load_curve --levels 120,200 --duration 30s`
（脚本自己拉起 uvicorn 子进程、跑完收摊；每个点断言"请求数 > 0"）。
环境：本机单 uvicorn worker、内存总线、mock 通道、模拟器开启；服务冷启动到 `/healthz` 可答 **1.12s**。

| 并发用户 | 请求数 | RPS | 整体失败率 | 聚合 P50/P95 (ms) | 演练 `drill_run` P95 | 站点清单 P95 | 就绪探针 P95 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 11 | 0.48 | 0% | 110 / 130 | 130 | — | — |
| 10 | 266 | 10.83 | 0% | 5 / 130 | 170 | 9 | 18 |
| 30 | 769 | 30.71 | 0% | 12 / 130 | 190 | 23 | 21 |
| 60 | 1363 | 54.32 | 0% | 32 / 260 | 710 | 95 | 100 |
| 120 | 2505 | 83.46 | 0% | 180 / 1000 | 2200 | 300 | 300 |
| 200 | 1918 | 63.82 | 2.50% | 850 / 4600 | 5800 | 1500 | 1300 |

判据与告警同源（`aegis.observability.load_policy`：只读端点 4000ms、一次完整演练 5000ms）。结论：

- 1→120 并发全程零失败，RPS 随并发近线性上升（10.83 → 83.46）；
- **拐点落在 120→200 之间**：RPS 不升反降（83.46 → 63.82），演练段 P95 从 2200ms 涨到 5800ms 越过预算，
  演练路径失败率 12.37%（整体 2.50%）；同期只读端点仍在预算内（1500/1300ms < 4000ms）。
- 所以这一版实现能对外承诺的容量口径是：**本机单 worker + 内存总线下，≤120 并发的"读 + 演练"混合负载全部达阈**。
- 口径边界：这不是 Linux + uvloop + 4 workers + NATS JetStream 的部署形态，部署环境那条曲线仍需重跑。

**这一轮的先决条件是压测档案本身能被修好**：`tests/load/locustfile.py` 里写着
`float(response.elapsed)`，而 locust 2.46 底下层是 httpx、`elapsed` 是 `timedelta` —— 每次任务都抛
`TypeError`，Locust 把它记成 task error，汇总表却是 `Aggregated 0 requests, 0(0.00%)` + 退出码 0。
也就是说这份档案从写下到今晚**一次请求都没发出去过，而它在 CI 里是绿的**。
阈值与判定口径因此挪进 `aegis/observability/load_policy.py`（locust 一 import 就 monkey-patch ssl，
测试进程没法 import 档案，判定逻辑必须住在产品包里才测得到），两侧钉住：
`tests/unit/test_load_policy.py` 测行为（耗时换算、等于预算判合格、预算与 `Settings` 同源），
`tests/perf/test_load_profile.py` 走 AST 测结构（档案必须调预算函数、
`float(response.elapsed)` 不得回潮、清单里列出的端点必须真的被任务打到——
`/readyz` 与站点清单原本就列了却没有任务打）。

**读数层自己也曾是"没被测过的那层"**：`scripts/load_curve.py` 把 locust 的两张表解析成结论，
这 100 多行解析/合并/零流量判定今晚之前没有任何用例。现在由
`tests/unit/test_load_curve_report.py`（14 项）钉住，fixture 是提交进仓库的**真 locust 2.46.6 输出**
（`tests/fixtures/locust_summary_locust-2.46.6.txt`，本机 2 并发 8s 原样落盘），
断言的数字全部来自它，而不是想象的表格长相。

同时记录两条不好听的：
1. `tests/unit/test_accuracy_replay_report.py` 落地时 `scripts` 不在 pytest 的 `sys.path` 上，
   收集期直接 `ModuleNotFoundError`——而 pytest 在收集阶段报错是**整场中断**，不是少跑一个文件。
   我在 `ab3c683`/`ccc800e` 两次推送前只跑了筛选过的命令，于是"全绿"这句话当时是错的。
   现在 `pyproject.toml` 显式 `pythonpath = ["."]`，并由
   `test_repo_hygiene.py::test_scripts_imports_are_backed_by_pythonpath` 守住这行配置；
   全量 `uv run pytest` 现为 **1839 passed / 49 skipped**。
2. `tests/unit/test_edge_zenoh_transport.py` 里"起任务后固定 sleep 20ms 再取 `session.gets[0]`"
   在整套会话里被别的任务挤掉过，随机 `IndexError`（单跑该文件三次全过，抓不到）。
   已改为条件等待 `_wait_for(...)`（带 2s 上限，超时判失败），并把三处采集类用例的响应窗口
   从 60–80ms 放到 400ms——那些用例断言的是"应答收得全"，超时边界另有专门的用例守。

重构后端到端复跑了一次真流量：`--levels 1,10 --duration 8s --port 8138`
→ users=1 requests=4 P50/P95=130/130ms、users=10 requests=84 P50/P95=7/170ms、零失败、退出码 0、
无残留服务进程。

## 还没测到的（诚实清单）

1. **部署形态的并发曲线**：上表是本机单 worker + 内存总线 + mock 通道；
   生产形态（Linux + uvloop + 4 workers + NATS JetStream + PostgreSQL 落库）的曲线、
   以及"按站数梯度"的资源占用还没量过。
2. **弱网工况**：Zenoh 链路已在真实运行时上验过互通、请求-响应、边缘存留与按序重放
   （见上表），但还没在真实丢包/高时延链路（4G/卫星回传）上量过；`poc_report.py` 的丢包梯度是
   本机注入的合成时延，不是空口实测。
3. **真实通道对接**（短信/北斗/广播）：目前 `delivery_mode=mock`，`warning_reach_ms` 里的 1.2s
   是 mock 适配器注入的模拟链路耗时，不是真实触达时延。
4. **图谱在线**：Graphiti/Neo4j 的读写分离与召回映射在单测里用替身驱动覆盖，
   还没在真实 neo4j 实例上跑过一次 `learn` + `recall`。
5. **离线底图与站点坐标的数据侧**：代码链路是通的（Cesium 自建 quantized-mesh 地形 + PMTiles 底图，
   零 Ion/谷歌依赖，前端有守卫用例禁止引入外部 token；`GET /api/v1/stations` 也已能返回真实清单），
   但数据还没烘焙：`frontend/public/basemaps/` 与 `public/terrain/` 目前只有 README，
   未挂瓦片时地图按"无底图 + 椭球地形"如实降级；站点坐标同理——平台侧没有任何生产代码向
   `monitoring_stations` 写站名/经纬度（`upsert_station` 只被测试与运维导入路径调用），
   所以地图上点位的多少取决于现场导入的站点台账，不是代码能力。
6. **案例库的经验侧**：`hazard_cases.json` 的 16 条是**自编预案模板**（骨架移植自 NexusMind 干预库，
   处置内容为川藏沿线实战口径），`confidence` 与 `estimated_delay_hours` 是编制判断值。
   召回、排序、图谱摄取三条路径都在真实数据上跑通了，但"预警准确率"意义上的**案例命中率**
   还没有可核对的真实事件复盘集可以做对照——这一项不能进任何"经验证于历史灾害"的表述。
   性质声明由代码与接口两处外显（`DATASET_PROVENANCE` 进 `/api/v1/integrations`、
   `source_note` 进每条召回命中），用例见 `tests/unit/test_knowledge_dataset_provenance.py`。
7. **预警准确率的数据侧**：算式与报表出口都已就位（`persistence/replay.py` +
   `scripts.metrics_report --dataset`，判据分支见上一节的三条真实命令），缺的是**数据**：
   仓库里没有真实现场标注的 JSONL（也不该造一个），所以"≥80%"这项至今是 `not_measured`。
   现场交付时要按 `{"dataset":{"kind":"field","source":…}}` + 逐案例
   `truth_warning/predicted_warning` 的口径导入，且总案例数须 ≥ `MIN_FIELD_CASES`（30），
   否则报表判"样本不足"——这一条同样是机器拦住的，不靠人记得。

上述七项补完后把命令、日期和输出摘要追加到上一节，并删掉对应条目。
