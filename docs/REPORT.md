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
| 文档侧嵌入（5 条真实案例正文） | 热态中位 3807ms | 属于建索引路径，不在预警请求路径上 |
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

## 还没测到的（诚实清单）

1. **压测绝对值**：Locust 的阈值是从 `Settings` 的 SLA 反推的（防止指标漂移），
   真实并发曲线需要在部署环境按站点数量梯度再量一轮。
2. **弱网链路**：Zenoh POC 的实测在 `backend/src/aegis/edge/poc_report.py` 产出的报告里，
   还没在真实丢包/高时延链路上跑过。
3. **真实通道对接**（短信/北斗/广播）：目前 `delivery_mode=mock`，`warning_reach_ms` 里的 1.2s
   是 mock 适配器注入的模拟链路耗时，不是真实触达时延。
4. **图谱在线**：Graphiti/Neo4j 的读写分离与召回映射在单测里用替身驱动覆盖，
   还没在真实 neo4j 实例上跑过一次 `learn` + `recall`。

上述四项补完后把命令、日期和输出摘要追加到上一节，并删掉对应条目。
