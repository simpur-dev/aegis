# AEGIS 平台架构说明（实现态）

> 本文只描述**已经在仓库里运行的代码**。规划中但未实现的部分显式标注状态，
> 避免文档领先于实现。权威目标架构见《课题6_项目架构设计》与根目录方案文档。

## 1. 分层与代码位置

| 层 | 职责 | 代码 |
| --- | --- | --- |
| L0 领域内核 | 枚举、契约镜像（与 `contracts/*.schema.json` 严格同构）、**定级结论 `RiskVerdict`**、**链路产物 `ChainResult` 族**、契约校验器、错误码、配置——**不 import 任何上层包**，所有人都可依赖它 | `domain/`、`contracts.py`、`errors.py`、`config.py` |
| L1 感知接入 | 多源采集、协议适配、时空归一、质量标记、**人工上报（群防群治）** | `connectors/`、`api/routers/drill.py`（`POST /api/v1/reports`）、`pipeline/report_intake.py`（`ReportIntake.submit`） |
| L2 数据层 | 遥测/预警/任务/链路运行态存储；图谱与向量检索（M2 接入 Neo4j+Graphiti）；**预警规则库（版本化三态）** | `backend/src/aegis/storage/`、`persistence/sql/005_trigger_rules.sql`、`persistence/rulebook.py` |
| L3 智能层 | 五大智能体（**外部实现**，经契约接入） | `contracts/`、`agents/`（交付落点）、`backend/src/aegis/agents/`（Mock 参考） |
| L4 服务层 | 触发规则、定级、任务拆解、**三路融合解析（规则/检索佐证/LLM）**、预警生成、靶向触达（mock + **真实 HTTP 通道**）、**语义交互（白名单动作 + 认知镜像）**、**阈值标定闭环**、链路编排、HTTP API | `services/`、`pipeline/`、`api/` |
| L5 应用层 | 纯 Web：态势总览 / 一张图 / 监测预警 / 预警发布 / 流程编排 / 指标量测 / **智能助手**（Vue3 + Vite + TS + AntD + ECharts） | `frontend/src/views/` |
| 基座 | 总线、能力注册、时延账本、指标导出、配置与日志 | `bus/`、`observability/`、`config.py`、`logging.py` |

## 2. 一次灾害事件的真实数据流

```
监测读数 ──IngestService(并发采集/单源超时隔离)──▶ TelemetryStore + 总线 data.<站>.<指标>
     │                                                     │
     │                                          遥测事件上总线（供智能体订阅）
     ▼
WorkflowChain.process(readings)   ← 唯一链路入口：DAG「标准五段」模板定序，段实现在 pipeline/stages.py
  ① perceive  : RuleEngine 规则命中（快路径） ⊕ 智能体 trigger_hit（若有）
  ② assess    : dispatch assess.hazard ──▶ 智能体定级 ──失败/超时──▶ RiskEngine 本地定级
  ③ plan      : dispatch plan.stu        ──▶ 智能体拆解   ──失败/超时──▶ TaskParser 剧本拆解
  ④ execute   : WarningService 生成预警 ──▶ dispatch execute.warn 或 DeliveryDispatcher 多通道并发下发
  ⑤ feedback  : dispatch feedback.attribute ──▶ 智能体归因（阈值建议 + 教训写记忆）──失败/超时──▶ 只归档回执
                两种结果都发 ops.feedback.status；采纳时那条事件与段台账一起带上效果分与归因正文
     │
     ▼
PlatformStore（预警/任务/链路结果）+ LatencyLedger（每段实测时延）+ Prometheus
```

关键点：**每一段都是"智能体优先、平台降级"**。智能体缺位时链路仍完整可用（`tests/e2e` 三形态测试证明），
智能体上线后按契约逐个替换降级路径，双方可并行开发。

## 3. 协同与契约

- 消息载体：`contracts/agent_message.v1.schema.json`（Pydantic 镜像在 `domain/messages.py`，二者由测试强制同构）
- 任务载体：`contracts/stu.v1.schema.json`
- Subject 规范、动作注册表、错误码、生命周期、一致性门禁：`contracts/AGENT_INTEGRATION_SPEC.v1.md`
- 网关职责：Schema 校验 → 动作白名单 → msg_id 幂等去重 → TTL 丢弃 → 协同事务台账 → 共享同步时延埋点
- 能力注册中心：能力声明 + 心跳失联摘除 + 按负载路由；**容量饱和时快速失败**（由上层降级/重调度显式处理，不做无界排队）

## 4. 性能设计

| 手段 | 位置 | 目的 |
| --- | --- | --- |
| 全链路 asyncio 协程 | `pipeline/`、`bus/` | 单进程内高并发，避免线程切换与 GIL 争用 |
| `asyncio.gather` 并发化 | 多区域链路、多通道触达、多源采集、并发事务 | 把串行等待变成并行（`tests/perf` 用相对基线断言防退化） |
| orjson 编解码 | `bus/transport.py` | 消息序列化吞吐（吞吐阈值由测试守住） |
| uvloop（非 Windows 自动启用） | `main.py` + `perf` extra | 事件循环吞吐 |
| FastAPI + uvicorn | `api/` | ASGI 异步接口层 |
| 环形有界集合 + 单调时钟计时 | `storage/`、`observability/tracer.py` | 长跑不涨内存、时延不受系统时钟回拨影响 |

## 5. 考核指标与量测出口

`PlatformContainer.latency_report()` 输出每个指标的样本数、P50/P95/P99、最大值、阈值与越限计数；
`scripts/metrics_report.py` 把它整理成判定表。未由本平台数据覆盖的指标（如预警准确率）显式标注 `not_measured`，
不用估算数字充数。

| 指标 | 埋点名 | 判定位置 |
| --- | --- | --- |
| 共享同步 ≤3s | `sync_agent_to_gateway_ms` | 网关 |
| 协同成功率 ≥90% | 事务台账 `TxnRecord.outcome` | 网关 |
| 调度响应 ≤2s | `stage_assess_ms` / `stage_plan_ms` | 链路分段 |
| 重调度 ≤10s | `collab_txn`（事务口径，M2 补 `reschedule` 专项） | 网关 |
| 接入 ≤5min | `ingest_end_to_end_seconds` | 摄取服务 |
| 人工上报接入 ≤5min | `report_intake_seconds` | `pipeline/report_intake.py`（`ReportIntake.submit`：文本→三路解析→同一条链路） |
| 语义交互一轮 | `assistant_reply_ms` | `services/assistant.py` |
| 预警生成 ≤3min | `warning_generation_ms` | 预警服务 |
| 靶向触达 ≤20min | `warning_reach_ms` | 触达/链路 |

## 6. 当前状态与下一步

已完成：

- **M1 内核**：契约、总线与网关、能力注册、规则/定级/拆解/预警/触达、降级链路、运行态存储、
  HTTP API、单元/契约/端到端/API/性能测试、容器编排与 CI。
- **M2 工作流**（ADR-0004 已落地）：DAG + 状态机引擎、版本化模板与实例、运行中改图、
  异常三段处置；注册节点类型 17 类（`chain_stage` 是内部原语，面板可拖 16 类）；Vue Flow 画布（`WorkflowView`）。
  调度响应与重调度时延为实测，见 `docs/REPORT.md`。
- **持久层**（ADR-0003 修订版）：PostgreSQL 17 + PostGIS + pgvector（asyncpg 直连、无 ORM）、
  有界写缓冲与重放去重；内存读视图保留，连接故障不换实现。
  站点清单经 `GET /api/v1/stations` 对外：坐标只认站点维表，未登记的站一律返回 `null`。
- **接入层三条腿**：模拟场站（演练/门禁）、MQTT 推送腿（站端 publish → 平台 subscribe，
  有界缓冲、满则丢最旧并计数）、公开气象拉取腿（形状不符即抛错，凭据不进状态面）。
  三条腿共用同一摄取服务，按源独立超时与失败隔离。
- **知识与检索**：Graphiti 双时态案例图谱（读写分离，LLM 只在写路径）、
  bge-m3 + bge-reranker int8/CPU 混合检索（dense + BM25 + RRF，LLM 不进检索回路），
  两者都接进预案生成链路并对外暴露只读接口。两条腿的**去哪儿取数**收在 `retrieval/port.py`
  一个索引端口后面：默认 `local`（pgvector + 进程内 BM25），可整体换成 seekdb
  （`VECTOR` + HNSW 余弦 ANN 与 ngram 中文全文同库），换引擎不改两腿语义与凭证结构。
  内置 16 条是**自编预案模板**（骨架移植自
  NexusMind 干预库），其性质与逐条出处随 `/api/v1/integrations` 与每条召回命中一起外显。
- **分析旁路**：ClickHouse 分钟级物化与 DuckDB 边缘单文件离线分析（write-behind，热路径不等 OLAP）。
- **可观测**：OpenTelemetry 链路（应用生命周期装配）+ 自研时延账本 + Prometheus 规则求值 +
  Alertmanager 分发与抑制；`GET /api/v1/integrations` 把九条腿
  （store/analytics/knowledge/retrieval/mqtt/weather/outbound/delivery/tracing）的启用与降级事实对外报出。
- **语义交互与人工上报（批次 B）**：`services/assistant.py` 四类任务（查询 / 预案问答 / 演练 / 上报）
  走白名单动作，执行类必须经 `/api/v1/assistant/confirm` 人工确认（一次性、带期、限本会话）；
  认知镜像复用 `rationale`/`degradations`/通道回执，LLM 只改措辞且**冒出事实之外的数字就整段弃用**。
  `POST /api/v1/reports` 与助手"帮我上报"共用 `ReportIntake.submit` 这一个入口，
  解析后进入的是**同一条链路**（同一 trace、同一量测、同一降级留痕）。
  低置信度（等级不是阈值命中所得）时 `_open_report_review()` 用 `REPORT_REVIEW_TEMPLATE`
  自动开一张停在 `human_review` 节点的核签工单，回执带回 `review{instance_id, pending_node,
  options, decision_endpoint}`，三态签核走**既有**的 `/api/v1/workflow/.../decision`；
  该模板刻意不进 `BUILTIN_TEMPLATES`（那份清单的口径是"5 灾种处置剧本"），也刻意不含
  `warning_publish` 节点——预警在进链路时已按判据发过，重复发布会把触达数字翻倍。
- **任务智能解析与规则库（批次 C）**：`services/semantic_parser.py` 三路融合
  （规则腿为唯一判据；检索佐证只加置信；LLM 只提建议且逐字段过白名单），冲突消解固定为
  规则 > 检索 > LLM 且三腿原始结论全部留痕；`trigger_rules` 表把阈值版本化
  （ADR-0006，同 `rule_id` 只允许一条 active），`services/calibration.py` 出分规则命中/误报对照与
  修订建议（`applied` 恒 false）。解析能力由 `tests/fixtures/report_parsing_cases.jsonl`（33 例）
  + `scripts/eval_report_parsing.py` 量出可复跑的准确率，合成集不自称官方口径。
- **一张图**：Cesium + 自建 quantized-mesh 地形 + PMTiles 离线底图（不依赖 Ion/谷歌），
  含离线与底图守卫的前端测试。
- **弱网链路（P1 POC）**：Eclipse Zenoh 站端↔网关通道，边缘有界缓冲 + 按序重放 +
  重放前可查（store/query at edge）；真运行时用例见 `tests/integration/test_edge_zenoh_live.py`。

未完成或待取证（诚实标注，完整清单以 `docs/REPORT.md` 的"还没测到的"为准）：

### 架构升级（MA/MB/MC）到哪一步了

《课题6_架构升级方案》的 §6 里程碑**从来没有在仓库里有过状态位**：方案本身仍是"草案，待评审"、
`adr/0007` 仍是"提议"，而 `AGENT_PROGRESS.md` 那张 M0–M4 表是**前端 UI 重设计**的里程碑，
与 MA/MB/MC 同名不同物。所以下面这张表是这份文档第一次替三个阶段记账——
判据只有一个：**代码在，且被生产路径调用**，才算做了。

| 阶段项 | 状态 | 证据 / 缺什么 |
| --- | --- | --- |
| §4.1 LLM 网关 v2（路由/结构化输出/用量账本/Prompt 注册表） | 已落地 | `services/llm_gateway.py`、`services/prompt_registry.py`；prompt 版本进 `model_info`，并由链路**逐段转记**到 `chain_runs.stages[].model_info`（2026-10-12 补齐：此前凭证只活在总线信封里，全仓 0 处读取，README 那句"随结论入库"是空的）。口径：智能体不声明就没有这个键（`exclude_none`，平台不代填 "unknown"），execute 段只在【文案真的进了预警】时记——被数字闸全拦回的那一程产出的是平台模板。读面：`GET /api/v1/audit/chain/{trace_id}` 直读库，实测重启后 `/api/v1/events` 读 0 条而它仍 `found=true` |
| §3 五智能体上总线（agentkit + 三态运行时） | 已落地 | `agents/`、`AEGIS_AGENT_RUNTIME=mock\|real\|off`，real 与 mock 互斥 |
| §5 验收口径分档（调度档 / LLM 档 / 端到端） | 已落地 | `sla_agent_txn_ms`，报表带 `口径档` 列；数见 REPORT |
| §4.5 D9 数据飞轮（采集侧） | 部分落地 | `human_review` 一条已通（核签→真值行，真库验过）；**助手确认、演练两条没接** |
| §4.3 D5 记忆服务 | 部分落地（四类门面于 2026-10-16 接上线并有了读者） | 共享命名空间已接生产（`007_agent_memory.sql` + 装配 + 治理端点）；四类记忆现在**两头都不空**：装配点把情景=案例知识腿、语义=混合检索腿接进门面，知识那一类**只在真配了 Graphiti 时**才接（拿案例库冒充图谱命中是这一节最容易被糊过去的地方），读者是 `GET /api/v1/memory/categories`——它把"这条腿没接"与"接了但这次没命中"分成两个字段回，而旧契约里两者都是一个 `[]`。**仍欠**：真图谱形态未取证（知识那一类只在真配了 Graphiti 时才有腿可读）；工具面那一条已于 2026-10-16 补上（`memory.categories`，`connected`/`items`/`note` 三态原样带出，见 §4.2 那一行） |
| §4.2 D4 MCP 工具层 v1 | 部分落地（工具面 7 项清单 + **研判取数已切** + 四类记忆已出得去 + 四档取证已做：容器形态、独立实现客户端、第三方 Inspector、逐工具时延） | 阶段 1 服务端：`mcp>=2.3` 作可选 extra（CI 与镜像都列了）、清单单一真源（共 7 项）、默认面 5 项只读（`telemetry.query`/`retrieval.search`/`cases.search`/`memory.search`/`memory.categories`；`simulation.run` 与写工具 `memory.write` 要显式点名，且实际能列出几项还取决于对应腿装配没有）、写工具两把钥匙、`/mcp/` 三态（404/503/streamable-http）、鉴权与数据 API 同面。阶段 2 取数切换：`AEGIS_AGENT_CONTEXT_FACE=auto\|rest\|mcp`（`auto` 跟着部署形态走，点名 `mcp` 而没有工具面则启动当场失败），`agents/mcp_context.py` 与 `rest_context.py` 复用同一份参数推导与同一条"什么算降级"的判据，状态行 `detail.agent_context_face` 分别说清"面开着"与"取数切过去了"。真栈两档对照见 REPORT MB-6；**"调用留 trace"于 2026-10-16 有了实现**：每次调用按工具留最近 200 条毫秒样本（状态行 `detail.latency_ms[工具]={count,p50,p95,max}`）+ 账本一条聚合线 `mcp_tool_ms`（`outcome=ok\|error`，失败也留样本，协议层按 schema 拒掉的不算一次调用）；`mcp_tools/server.py` 也因此进了时钟守卫的 `MEASURING_FILES`。§4.3 四类记忆经 `memory.categories` 出得来，`connected`/`items`/`note` 三态原样带出并附 `disconnected` 名单。**取证**（详见 REPORT 同名一节）：只用 httpx 的独立 JSON-RPC 客户端与官方 SDK 客户端在同一次部署里读数同档（容器形态服务端 p50 0.010—0.178 ms、客户端墙钟 p50 6.27—7.33 ms——差值几乎全是传输与握手，不是取数）；容器里令牌三档实测无/错=401、对=200。**仍欠**：① 第三方客户端只测到 MCP 项目自家的 Inspector CLI 一种产品（它列出 5 项、`annotations` 解读正确、`cases.search`/`memory.categories` 出真读数，三种记忆现场在外链上仍是三种；还撞出它把纯数字实参猜成 int 而被我们 schema 层拒——同一条纪律在第三方客户端上成立），各家客户端对 annotations/分页/资源面的实现差异未测；② `retrieval.search` 那一路的真检索腿在两次取证里都没装配（要嵌入模型/服务），它只在用例注入替身的形态下绿过；③ **perceive/plan 没有"自己取数"这条腿可切**——平台在派发时就把上下文塞进信封（`pipeline/stages.py:441-442` 的 `reference_cases`/`retrieved_context`），把自家进程内的服务调用绕一圈 HTTP 是倒退；工具面对这两段的真实意义是给**独立进程的智能体与外部客户端**一个标准口拿同一批能力（沙盒经 `simulation.run` 已是这种消费方）；④ `mcp_tool_ms` 没设考核线、没进告警；⑤ 工具面这层没接 OTel 跨度，只有账本 |
| §3.2 assess 双盲仲裁 | 部分落地（2026-10-15 补齐"并行两腿"与"分歧进台账"） | 触发/一致上调/仲裁/跳过留痕都在；**两条腿现在真的同时发车**——触发条件（初始定级 ≤2）长在模型的第一份结论上，那是第二腿发车前拿不到的，所以预触发用的是平台 dispatch 前就有的事实（`rule_level` 随请求交给智能体），规则轨没报红/橙时仍按原口径在模型报出后**补跑**第二腿：覆盖面不减，只是那一程串行；`double_blind_mode`（parallel/serial）与 `disagreement`（三态：一致/不一致/老应答没带这一格）都进了研判段的 note，也就是进了 `chain_runs` 台账。**增强腿失败已收口在增强腿**（MB-7c）：第二遍或仲裁任一异常都不再穿出 `handle()`，第一遍结论照用、状态写进 `double_blind=failed_<leg>:<异常名>`。**仍欠**：分歧率的可统计口径（现在只在段注里，不是能聚合的一列）、定级一致率与"建议人工核签"门限的金标实测（见 §3.2 验收行） |
| §3.4 execute 三分支文案 | 已落地 | `audience_copies` 入库（`006` 迁移）；发送权仍在平台交付腿 |
| §3.5 feedback 归因闭环 | 部分落地（派发于 2026-10-15 接上；阈值建议→人工确认→规则库新版本于 2026-10-16 接上） | 链路在汇总触达回执后 dispatch `feedback.attribute`，采纳的归因/效果分/阈值建议一起进段台账与 `ops.feedback.status`，教训由内环写共享记忆。修前的形状是"五个内环都在册、feedback 一个请求也收不到"：该段只广播不派发，于是 §3.5 的三件事在真栈上发生 0 次（2026-10-15 真库读回 `agent_memory` 里 lesson 恒 0 行），而单测直调 `handle()` 全绿看不出来。判据：效果分缺席或不在 0—1 内整份弃用并留痕；`decided_by=local` 那一程不记模型凭证；反馈腿失败只塌自己，已发布的预警不收回。**仍欠**：阈值建议的闭环已于 2026-10-16 接通（入账 → 人工确认 → `trigger_rules` 新版本 → 引擎装载，见 `services/threshold_suggestions.py` 与 `sql/008_threshold_suggestions.sql`），`human_review_suggested` 也已于同日有了读者（`RiskVerdict` 一列事实 + `GET /api/v1/review/pending`，签没签过看 `warning_truth_labels` 那张真表）；**仍欠的是外显**——这两份清单前端都看不见，值班员目前只能经接口取数 |
| §3.6/§4.4 推演沙盒 | 部分落地（2026-10-16 接上角色内环、汇聚器、DAG 节点与报告页） | `services/simulation.py` 的 K×T/概率矩阵/超限降 K 口径不变；角色侧搬到新包 `simulation/roles.py`：四个 ephemeral 角色（指挥员/救援队/受影响群众/监测员）每轮各出一票，`RoleAction` **只有档位与理由、没有数值字段**（结构上不能发明数字，这比在提示词里恳求"别编数"可靠），白名单逐字写进提示词（assess/plan 都栽过"只说取其一"结果模型回中文描述）；`RulePolicy` 是默认档（离线可复算），`LlmRolePolicy` 要 `AEGIS_SIMULATION_ROLES_LLM=true` 且网关在位，单次抖动只塌那一轮并记一次 `fallbacks`、不把整份报告拖成 503，出口回来的东西再过一次 schema；`aggregate_actions` 把四票折成本轮生效预案（多数票、平票取缓解更高的一档、白名单外的票丢弃）——票只决定"用表里哪一行冻结系数"，不决定系数本身。报告新增 `roles` 一格（policy / 在场角色与立场 / 应问·实问·回落 / 漏接时自带 note），DAG 节点 `situation_simulate` 经装配点注入的 `sandbox_runner` 带出 `output.sandbox`（未注入或失败只降级那一格并写明原因，画布参数逐个夹进区间），前端 `/simulation` 报告页把矩阵/时间线（每个在场角色一列）/预案对比/角色档摊开，并在边界上拒绝缺键报告。验收口径仍是 §3.6 那句"矩阵正确性抽查、演示非 SLA"，概率/缓解系数为未实测初值。**仍欠**：真 LLM 档只量到 T=2（8 次调用、6.62 s、零回落、`task=simulation` 首过率 1.0；同参数下规则档 0.0135 s，
同一泥位上两档给出 0.1029 与 0.1821 两个触发率——票改表、模型不改数），T=5 与 K 拉满的墙钟/成本未量；
这一页未纳入 `e2e/ui-overlap` 的逐页版式门禁（真机点验 21 项只覆盖浅色主题一屏） |
| §4.7 assistant 意图解构 | 部分落地 | 多步分解在 `/assistant/plan`；`/chat` 仍是词表优先，前端没调 plan |
| §4.8.1 服务令牌 | 已落地（默认关是配置项，不是缺口） | 数据 API 与 MCP 工具面共用同一个 `_presented_token`，认 `X-Aegis-Token` 与 `Authorization: Bearer` 两种头（头名不一致曾让"配了令牌"与"真的在拦"脱钩，那正是这一节的原始事故）；**智能体真在请求的读端点也进了闸**（`/api/v1/cases/recall`、`/api/v1/retrieval/search`、`/api/v1/memory/shared` 读写删、新增的审计直读面）；闸的形状已从"11 条路由手工挂 `dependencies=`"改成一条 HTTP 中间件，口径只有"路径在 `/api/` 下就要凭据"（`/healthz`、`/readyz`、`/metrics`、`/docs` 不吃闸，`/mcp/` 自成一闸、共用同一枚令牌），判据跟着从路由表取 28 条业务路径逐条指认——新增端点默认被覆盖，"漏挂"这件事结构上不可能再发生；2 条变异（闸一律放行 / 闸永远拒绝）各红一条用例。**Web 端凭据也已接上**：每页内容底部的"服务令牌"一行粘贴一次（存本机 localStorage），6 个 api 模块的 axios 拦截器与两处 `fetch`（事件流、助手 SSE）都带 `X-Aegis-Token`，被 401 时那一行红着提示重填；5 条前端变异（漏装拦截器 / 两处 fetch 不带凭据头 / 存了令牌却不发头 / 401 不吭声）各红一条用例。代价说死：令牌明文存在这台浏览器的 localStorage 里，共用机器上别拿它当账号体系用 |
| §4.5 评测服务与金标集 | 部分落地 | 门禁与出口在（`scripts/quality_gate.py`，`ci.yml` 跑）；表已加第五列**结构化输出首过率**并用真 DeepSeek 实测入基线（0.7838，按 task 分开看才是能行动的那张表）；**另三列仍为 null**：无 `eval/judge.py`、无金标数据 |
| §4.6 D7 编排归一（chain 退役） | **已落地（四步走完，2026-10-16 收口）** | 段的判定与段的记账一起收进 `pipeline/stages.py`（575 → 756 行，编排的唯一入口是 `run_stage(段名, 盘面)`），根跨度收进 `pipeline/span.py`，跨段中转收进 `pipeline/board.py`，产物形状收进当时的 `pipeline/result.py`（2026-10-16 结构重构 P1 已下沉为 `domain/chain.py`，理由见那一行）；工作流侧新增 `chain_stage` 节点（内部原语：引擎注册 17 类、面板可拖 16 类）与"标准五段处置链路"模板（节点超时与 `sla_agent_txn_ms` 同源），`pipeline/workflow_chain.py` 用 DAG 跑同一条链；`chain.py` 859 行（D7 立项实测）→ 321（第一拍）→ 91（第三拍）→ 删除（本拍），只剩"按顺序调段 + 根跨度 + 收盘"。**parity harness 10 条全绿**：同一容器、同一个 `HazardStages`，固定顺序与 DAG 逐项对齐段序/mode/note、定级、任务单元、预警内容、降级与故障清单、D10 凭证落段、以及五段 `stage_*_ms` 各加一次；变异检查打在**单侧**（parity 比的是两条路径的差，改共享实现两边一起变、它本就不该红）——DAG 丢早退红 1 条、DAG 不补 skipped 红 1 条、固定顺序少跑反馈段红 3 条。**第 4 步（本拍）**：`pipeline/chain.py` 已删，`container.chain` 就是 `WorkflowChain`，原有 6 个调用点（`api/app.py` 1、`container.py` 3、`scripts/drill.py` 与 `scripts/metrics_report.py` 各 1）一行没改——"退役不改口径"靠的是形状沿用，不是承诺。护栏换了形式：parity 用例退役成 `tests/unit/test_chain_orchestration_contract.py`（12 条），把当初两路径对齐时量出来的可观测物写成**字面基线**（段序/mode/note、定级、任务单元、预警正文、降级与故障清单、五段样本各 +1、多区域只多一条感知样本），5 个直构链路的测试文件改用一个 `conftest.chain_entry`（真引擎 + 真模板，刻意不留"单测专用的简化路径"）。真栈取证（退役后复跑）：NATS JetStream + Postgres 上跑一条链路，从 `chain_runs` 读回的五段（名/mode/ok/note）与定级五元组逐项等于那份字面基线，引擎留底里五个节点都是 `chain_stage` 且 succeeded，跑完盘面归零；反向对照（故意把基线改一处）当场报"不等"并退出 1。`/readyz` 新增 `chain` 一块（`open_boards`/`traces`/`held`/`capacity`/`evicted`），盘面漏收当场可见。8 条变异全部有牙（三条第一轮是空转或外壳在骗人，换判据/换锚点后才红，过程记在 REPORT）。**仍欠**：工作流实例账本本身仍未落库（`save_workflow_instance` 零调用方，ADR-0003 的既定后置项 → 任务 #53） |
| MC 模型线（QLoRA/GPTQ/训练集导出/对照实验/论文专利） | 未开工 | 无任何落点 |

一句话（口径与上表逐行对齐，不另算一套）：**MA 已收口**（欠的那个"结构化输出首过率"于 2026-10-12 实测补齐，
并顺带抓出空内容绕过修复轮的真缺陷）；**MB 八项 = 2 整 + 6 部分**：execute 三分支文案、服务令牌鉴权面已落地（后者于 2026-10-16 补齐整面：闸改成一条覆盖 `/api/*` 的中间件，Web 端也开始携凭据，见 §4.8.1 那一行），
反馈闭环的阈值建议那一半于 2026-10-16 接通（入账→人工确认→规则库新版本→引擎装载，缺外显与 `human_review_suggested` 的读者，见 §3.5 那一行），
双盲两点（并行发车、分歧进台账）于 2026-10-15 补齐但只到装配级证据（真模型下省了多少墙钟未量），
记忆门面的四类接线与读者于 2026-10-16 补上（缺真图谱形态取证与 MCP 暴露，见 §4.3 那一行），
推演沙盒的角色内环/汇聚器/DAG 节点/报告页四项于 2026-10-16 接上，真 LLM 档也量过一次（T=2 规模，见 §3.6/§4.4 那一行），
评测与 assistant 两项各差后半截，MCP 工具层已从"整项未开工"做到 v1 主体（服务端 + 研判取数已切 + §4.3 四类记忆有了工具面出口 + "调用留 trace"于 2026-10-16 真的留了时延；
容器形态、独立实现客户端与第三方 Inspector CLI 都握过手（第三方只测到这一种产品），检索腿现场仍未取证。
方案消费方表里"perceive/plan 也要经它取数"这一条经核是**伪需求**：链路在派发时就把上下文塞进信封
（`pipeline/stages.py:441-442`），把自家进程内调用绕一圈 HTTP 是倒退，2026-10-16 更正口径）；
围栏补齐与 D9 采集侧属于 §4.8/§4.5 的账，不在这八项内。**MC 只动了编排归一这一项**（§4.6 四步走完：
段逻辑与记账下沉、标准五段模板与 DAG 路径、parity 全绿、`chain.py` 于 2026-10-16 删除，
护栏换成形状契约用例），QLoRA/GPTQ、训练集导出、
基线对照实验、论文与专利交底书都未动。
MB 四条过门标准目前：**防注入用例过 = 已达**（通道级 6 条 + 变异 8 条）；质量表首批基线 = 半条
（五项里 STU合法率与首过率有数，另三列等金标集与裁判凭据）；推演演示可跑 = **这一条已达，口径要说准**（沙盒经 REST 与 MCP
两个门面出同一份报告，DAG 节点带 `sandbox` 一格，前端 `/simulation` 页真机点验 21 项全过：角色四列票、降档说明、
"未实测初值"那句都在页面上；LLM 档也真跑过一次（T=2、8 次调用、6.62 s、零回落、首过率 1.0），
只是默认仍是 `rule` 档——可复现基线不该由模型票来决定）；MCP 切换后门禁复绿 = **切换这一步已达，口径要说准**：
`AEGIS_AGENT_CONTEXT_FACE=auto`（默认）在工具面在位时即走 MCP，全量门禁是在"没装 `.env`、工具面关着"的形态下取的绿
（此时 auto 解析成 rest，即兼容面），`face=mcp` 那一档由定向用例 + 真栈两档对照覆盖；
"把整条全量在 mcp 形态下再跑一遍复绿"没做，也没法做——那会让"默认形态"断言集体改口径，不是同一件事。


- **真实并发曲线**：Locust 阈值由 `Settings` 的 SLA 反推，绝对值仍需在部署环境按站点数量梯度再量。
- **弱网工况**：Zenoh 已在真实运行时验过互通与按序重放，但丢包/高时延数字来自本机合成注入，
  不是空口实测。
- **真实通道对接**：交付侧 `delivery_mode=http` 已接线（主机白名单闸 + 凭据只进请求头 + 回执入库），
  但缺真实网关地址与凭据，所以现场触达时延仍未测得；当前 `warning_reach_ms` 的 1.2s 是
  mock 适配器注入的模拟链路耗时，两种口径在报表里分开标注。
- **工作流模板**：内置 5 灾种模板（批次 D2）；`situation_simulate` 已改为案例驱动多情景（D3），
  知识腿缺席时显式 `degraded` 而不是回退到 ±1 启发式。
- **带 LLM 的解析与润色路径**：代码与替身级用例就位，但没有凭据就没有数字（见 REPORT 诚实清单）。
- **图谱在线**：Graphiti/Neo4j 的读写分离在单测里由替身驱动，还没在真实 neo4j 实例上跑过
  一次 `learn` + `recall`。
- **数据侧欠的三样**：离线底图/地形只烘了**合成样本**（`scripts/build_offline_tiles.py`，0–2 层完整金字塔，
  已被真 pmtiles/Cesium 读取路径与镜像解码器验过；欠的是合规的真实 DEM/影像烘到 0–15 覆盖西藏范围）；
  站点台账的入口已就位（`python -m scripts.import_stations --source stations.csv`：全量校验后才写、
  不猜坐标、内存视图默认拒绝），欠的是现场那份**真实台账文件**本身；
  预警准确率缺**现场标注案例集**——算式与报表出口（`scripts.metrics_report --dataset`）都已就位，
  没有 `kind=field` 的数据集就永远如实标 `not_measured`。
- **平台侧总线未替换**：Zenoh 只做站端↔网关这一段，灾害总线仍是 NATS JetStream。

复现命令与数字口径统一收录在 `docs/REPORT.md`。
