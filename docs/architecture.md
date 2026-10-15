# AEGIS 平台架构说明（实现态）

> 本文只描述**已经在仓库里运行的代码**。规划中但未实现的部分显式标注状态，
> 避免文档领先于实现。权威目标架构见《课题6_项目架构设计》与根目录方案文档。

## 1. 分层与代码位置

| 层 | 职责 | 代码 |
| --- | --- | --- |
| L1 感知接入 | 多源采集、协议适配、时空归一、质量标记、**人工上报（群防群治）** | `connectors/`、`api/app.py`（`POST /api/v1/reports`）、`container.submit_report` |
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
HazardResponseChain.process(readings)
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
| 人工上报接入 ≤5min | `report_intake_seconds` | `container.submit_report`（文本→三路解析→同一条链路） |
| 语义交互一轮 | `assistant_reply_ms` | `services/assistant.py` |
| 预警生成 ≤3min | `warning_generation_ms` | 预警服务 |
| 靶向触达 ≤20min | `warning_reach_ms` | 触达/链路 |

## 6. 当前状态与下一步

已完成：

- **M1 内核**：契约、总线与网关、能力注册、规则/定级/拆解/预警/触达、降级链路、运行态存储、
  HTTP API、单元/契约/端到端/API/性能测试、容器编排与 CI。
- **M2 工作流**（ADR-0004 已落地）：DAG + 状态机引擎、版本化模板与实例、运行中改图、
  异常三段处置；注册节点类型 16 类；Vue Flow 画布（`WorkflowView`）。
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
  `POST /api/v1/reports` 与助手"帮我上报"共用 `container.submit_report` 这一个入口，
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
| §4.3 D5 记忆服务 | 部分落地 | 共享命名空间已接生产（`007_agent_memory.sql` + 装配 + 治理端点）；**四类门面无人调**，消费者等 §4.2 |
| §4.2 D4 MCP 工具层 v1 | 部分落地（工具面 + **assess 一段**的取数已切；其余四段与沙盒消费方未接） | 阶段 1 服务端：`mcp>=2.3` 作可选 extra（CI 与镜像都列了）、清单单一真源、默认面 4 项只读、写工具两把钥匙、`/mcp/` 三态（404/503/streamable-http）、鉴权与数据 API 同面。阶段 2 取数切换：`AEGIS_AGENT_CONTEXT_FACE=auto\|rest\|mcp`（`auto` 跟着部署形态走，点名 `mcp` 而没有工具面则启动当场失败），`agents/mcp_context.py` 与 `rest_context.py` 复用同一份参数推导、同一条"什么算降级"的判据，状态行 `detail.agent_context_face` 分别说清"面开着"与"取数切过去了"。真栈两档对照见 REPORT MB-6（face=mcp 时工具面台账 `calls={'cases.search':1}`、face=rest 时 `calls={}`）；方案那条过门标准"MCP 切换后门禁复绿"= **已达**。**还欠的**：方案那张消费方表里 perceive/plan 也要经 `cases.search`、沙盒经 `simulation.run`，如今只有研判切了；写工具与容器/第三方客户端形态未取证 |
| §3.2 assess 双盲仲裁 | 部分落地 | 触发/一致上调/仲裁/跳过留痕都在，但两次评估是**串行**而非方案的"并行"；`disagreement` 写进信封后链路丢弃；**增强腿失败已收口在增强腿**（MB-7c，2026-10-15）：第二遍或仲裁任一异常都不再穿出 `handle()`，第一遍结论照用、状态写进 `double_blind=failed_<leg>:<异常名>`，链路把它转成一条看得见的降级记录进 `chain_runs.degradations`；关键路径（第一遍定级）失败仍按原语义上抛交规则降级轨 |
| §3.4 execute 三分支文案 | 已落地 | `audience_copies` 入库（`006` 迁移）；发送权仍在平台交付腿 |
| §3.5 feedback 归因闭环 | 部分落地（派发于 2026-10-15 接上） | 链路在汇总触达回执后 dispatch `feedback.attribute`，采纳的归因/效果分/阈值建议一起进段台账与 `ops.feedback.status`，教训由内环写共享记忆。修前的形状是"五个内环都在册、feedback 一个请求也收不到"：该段只广播不派发，于是 §3.5 的三件事在真栈上发生 0 次（2026-10-15 真库读回 `agent_memory` 里 lesson 恒 0 行），而单测直调 `handle()` 全绿看不出来。判据：效果分缺席或不在 0—1 内整份弃用并留痕；`decided_by=local` 那一程不记模型凭证；反馈腿失败只塌自己，已发布的预警不收回。**仍欠**：阈值建议只到 `pending_human_review`，"人工确认→calibration 复用"这条落地还没接（任务 MB-6 后半截），前端也看不见归因 |
| §3.6/§4.4 推演沙盒 | 部分落地 | `services/simulation.py` 有 K×T/概率矩阵/超限降 K；**角色只是四个字符串、无 LLM**，DAG 节点未换成沙盒，前端无报告页 |
| §4.7 assistant 意图解构 | 部分落地 | 多步分解在 `/assistant/plan`；`/chat` 仍是词表优先，前端没调 plan |
| §4.8.1 服务令牌 | 已落地（默认关是配置项，不是缺口） | 数据 API 与 MCP 工具面共用同一个 `_presented_token`，认 `X-Aegis-Token` 与 `Authorization: Bearer` 两种头（头名不一致曾让"配了令牌"与"真的在拦"脱钩，那正是这一节的原始事故）；**智能体真在请求的读端点也进了闸**（`/api/v1/cases/recall`、`/api/v1/retrieval/search`、`/api/v1/memory/shared` 读写删、新增的审计直读面），11 条变异逐条证明"摘掉任一端点的鉴权都会红"。仍欠一件：Web 端不携凭据，所以开令牌的部署里数据面板会 401（README 2.3 第 11 条） |
| §4.5 评测服务与金标集 | 部分落地 | 门禁与出口在（`scripts/quality_gate.py`，`ci.yml` 跑）；表已加第五列**结构化输出首过率**并用真 DeepSeek 实测入基线（0.7838，按 task 分开看才是能行动的那张表）；**另三列仍为 null**：无 `eval/judge.py`、无金标数据 |
| §4.6 D7 编排归一（chain 退役） | 未开工 | `pipeline/chain.py` 859 行仍在（2026-10-15 本机实测行数；反馈腿接上派发后又长了 86 行——退役前它一直在长），无"标准五段"模板、无 parity harness；触达段两处实现口径不一 |
| MC 模型线（QLoRA/GPTQ/训练集导出/对照实验/论文专利） | 未开工 | 无任何落点 |

一句话（口径与上表逐行对齐，不另算一套）：**MA 已收口**（欠的那个"结构化输出首过率"于 2026-10-12 实测补齐，
并顺带抓出空内容绕过修复轮的真缺陷）；**MB 八项 = 2 整 + 6 部分**：execute 三分支文案、服务令牌鉴权面已落地（后者今日补齐了"智能体自己用的那两个读端点没挂闸"这一半，见 §4.8.1 那一行），
记忆/双盲/推演/评测/assistant 五项各差后半截，MCP 工具层已从"整项未开工"做到 v1 主体（服务端 + 研判取数已切，
方案消费方表里 perceive/plan/沙盒那几路还没接，2026-10-12）；
围栏补齐与 D9 采集侧属于 §4.8/§4.5 的账，不在这八项内。**MC 未动**。
MB 四条过门标准目前：**防注入用例过 = 已达**（通道级 6 条 + 变异 8 条）；质量表首批基线 = 半条
（五项里 STU合法率与首过率有数，另三列等金标集与裁判凭据）；推演演示可跑 = 半条（沙盒经 API 出口能出
概率矩阵，但 DAG 的 `situation_simulate` 仍是旧实现、前端无报告页）；MCP 切换后门禁复绿 = **切换这一步已达，口径要说准**：
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
