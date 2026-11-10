# AEGIS 智能体接入规范 v1.0（Agent Integration Spec）

> 状态：**待双方评审冻结**（M1 第一里程碑）
> 适用范围：平台侧（L1/L2/L4/L5 + 基座）与智能体侧（L3 五大智能体）之间的全部交互
> 契约文件：`contracts/agent_message.v1.schema.json`、`contracts/stu.v1.schema.json`
> 变更规则：语义化版本；v1 冻结后新增字段只能是可选字段；破坏性变更走 v2 并行 subject，v1 保留至少一个学期

---

## 0. 边界三原则

1. **智能体是总线上的独立服务**：不 import 平台代码、不直连平台数据库、不依赖平台内部实现细节。
2. **唯一交互载体是 AgentMessage**：任何跨边界交互必须落成一条合法 AgentMessage，由平台总线网关校验。
3. **数据经引用而非内嵌**：载荷 ≤64KB；大体积数据放 `refs`，由智能体经平台数据 API 取回。

违反任一条的实现无法通过一致性测试（§7），不得接入生产总线。

---

## 1. 消息载体：AgentMessage v1

关键字段语义（完整约束见 schema）：

| 字段 | 语义 | 架构指标关联 |
|---|---|---|
| `msg_id` | 全局唯一 + **幂等键**：重复投递必须被去重且无副作用 | 弱网可靠重传（架构文件 §6.2） |
| `trace_id` | 一次灾害事件全链路共享（感知→研判→决策→执行→反馈） | 指标归因、Jaeger 对接 |
| `ts` | 发送方 UTC 毫秒时间戳 | **共享同步 ≤3s 的度量起点** |
| `kind` | `request`/`response`/`event`/`error` | `request→response` 构成**一次协同事务** |
| `deadline_ms` | request 必填，事务完成期限 | 超期计失败事务 → 协同成功率 |
| `priority` | 1（最高，可抢占）~5 | 冲突仲裁与调度排序 |
| `ttl_ms` | 超期未消费由网关丢弃 | 弱网防过期指令执行 |
| `refs` | 数据引用（type+id） | 数据访问边界（§4） |

## 2. Subject 规范

NATS JetStream，命名一律小写、点分、层级固定：

| Subject 模式 | 方向 | 说明 |
|---|---|---|
| `data.<source>.<metric_type>` | 平台 → 总线 | 遥测/上报数据流。例：`data.rain_gauge.rainfall_10min` |
| `agent.<type>.in` | 平台 → 智能体 | 任务下发（request）。type ∈ perceive/assess/plan/execute/feedback |
| `agent.<type>.out` | 智能体 → 平台 | 结论上行（response / event） |
| `agent.<type>.hb` | 智能体 → 平台 | 心跳与能力声明（event） |
| `workflow.<instance_id>.<event>` | 引擎内部/可观测 | 节点生命周期事件（started/finished/failed/rescheduled） |
| `platform.alert.<level>.<region>` | 平台 → 总线 | 已确认的告警广播（供大屏/订阅） |
| `ops.<component>.<signal>` | 双向 | 运维信号（degrade、heartbeat_lost 等） |

Stream 配置约定：`DATA`（subjects `data.>`，max_age 7d）、`AGENT_IN`、`AGENT_OUT`、`WF`（`workflow.>`）。智能体消费组（durable consumer）命名 `cg_<agenttype>_<instance>`，必须使用 **工作池（work queue）** 语义避免重复处理；同一 `msg_id` 重投递由接收方去重。

## 3. 动作注册表（v1 冻结集）

未注册动作默认被网关拒绝（`E_UNREGISTERED_ACTION`）。

> **两种"没人接"要分清**：动作**登记在契约里但没有智能体声明承接** → 网关记 `no_agent`
> （不是 error，平台按本地降级轨走）；动作**根本不在上面的注册表里** → `E_UNREGISTERED_ACTION`
> （这是实现缺陷，会进 error 通道）。例：外部交付实现当前只注册 `assess.hazard`，
> 其余四段记 `no_agent`，不是故障。

| 动作 | 发起方 | 方向 | payload 必填字段 | 说明 |
|---|---|---|---|---|
| `perceive.anomaly` | perceive | out→event | `station_id`,`metric`,`value`,`observed_at`,`anomaly_score` | 弱信号/越限标注上报 |
| `perceive.trigger_hit` | perceive | out→event | `rule_id`,`hazard_type`,`region_code`,`evidence_refs` | 触发条件命中（指标1 溯源依据） |
| `assess.hazard` | platform→perceive/assess | in→request | `region_code`,`window`,`hazard_type`（assess 侧必填） | 请求灾种识别。perceive 侧可附 `readings`（本批读数）；应答为 `trigger_hits`；assess 侧 payload 为 `assess.v1` 请求（见 §9），应答为 `assess.risk_level` / `assess.v1` 响应 |
| `assess.risk_level` | assess | out→response | `hazard_type`,`region_code`,`risk_level`(1-5),`confidence`,`rationale`,`evidence_refs` | 定级结论 |
| `plan.stu` | platform→plan | in→request | `event_id`,`risk`,`objective`,`constraints` | 请求任务拆解 |
| `plan.stu_result` | plan | out→response | `task_units`(STU 数组，须过 stu.v1 校验) | 返回标准化任务单元集 |
| `plan.reschedule` | platform→plan | in→request | `workflow_instance_id`,`anomaly`,`current_topology` | 异常工况重调度建议（≤10s） |
| `execute.warn` | platform→execute | in→request | `warning_id`,`level`,`regions`,`audiences`,`content_refs` | 预警发布指令 |
| `execute.ack` | execute | out→response | `warning_id`,`channel_results`(每通道 delivered/pending/failed) | 发布结果回执 |
| `feedback.status` | feedback | out→event | `warning_id`,`reach_stats`,`response_state`,`effect_score` | 响应/触达/效果反馈（**只广播、不应答**；要应答的是下面那个） |
| `feedback.attribute` | platform→feedback | in→request | `warning_id`,`region_code`,`reach_stats`,`response_state` | 链路汇总触达回执后的**归因请求**（要应答）。与 `feedback.status` 分开口径：前者要回执、后者只是广播——混用一个动作会让"谁该回这条"没有判据 |
| `agent.register` | 智能体 → 平台 | out→event | `agent_id`,`registration` | 注册与能力声明（生命周期，见 §4） |
| `agent.heartbeat` | 智能体 → 平台 | out→event | `agent_id` | 心跳（生命周期，见 §4） |
| `error.raise` | 双向 | out→error | `code`,`message`,`retryable` | 失败回执（见 §5） |

**关于 `feedback.attribute` / `agent.register` / `agent.heartbeat`**：这三个动作自 v1 起就存在于
`aegis.domain.enums.Action` 并在真链路上派发，只是这张表此前漏记（2026-11-10 补）。
**这是文档补齐，不是契约变更**——冻结集以 `Action` 枚举为准，表是它的可读镜像。

**风险等级统一口径**：`risk_level` 1-5 对应红/橙/黄/蓝/无（1 最高），所有智能体与平台共用，禁止自定义分级。

**感知命中的合并口径**：链路对 `trigger_hits` 按 `rule_id` 与本地规则命中去重合并。请求/应答
（`assess.hazard` 的应答）只采纳与本链路同 `region_code` 的命中；主动事件 `perceive.trigger_hit`
只有 `trace_id` 与本链路相同且在存活窗内才被采纳——同一区域的并发链路不会互相吞并命中。
`perceive.anomaly` 是单点标注、无 `rule_id`/`hazard_type`，不参与命中合并。

## 4. 数据访问边界

智能体取数只能走平台数据 API（只读，服务令牌鉴权）：

- `GET /api/v1/telemetry` — 时序查询（站点、指标、时间窗，支持 `since`/`until`；缺时区或逆序窗返回 422 `E_BAD_WINDOW`，响应形状见 OpenAPI 的 422 声明）
- `GET /api/v1/assess/evidence?region_code=&window_minutes=` — 研判证据包（`assess.v1` `AssessEvidence`，与推送腿同构）
- `GET /api/v1/site-profiles` / `GET /api/v1/site-profiles/{region_code}` — 区域级静态孕灾画像
- `GET /api/v1/graph/entities` / `POST /api/v1/graph/search` — 灾情知识图谱与 GraphRAG 检索
- `GET /api/v1/cases` — 历史案例库
- `GET /api/v1/warnings` / `GET /api/v1/tasks` — 预警与任务单元
- 实时流：订阅 `data.>`（不查询数据库）

平台侧提供数据 API 的动机：使指标3 的量测点（写入→感知时延）可观测，并保证 L2 数据层可独立演进（智能体不受表结构变更影响）。

## 5. 错误与超时语义

| 码 | 含义 | 是否可重试 |
|---|---|---|
| `E_SCHEMA_INVALID` | 消息不合契约（网关拒收，附错误路径） | 否 |
| `E_UNREGISTERED_ACTION` | 动作未注册 | 否 |
| `E_TIMEOUT` | request 超 `deadline_ms` | 是 |
| `E_NO_CAPABLE_AGENT` | 无匹配能力的在线智能体 | 是（退避后） |
| `E_INTERNAL` | 智能体内部异常 | 视情况 |
| `E_DUPLICATE` | msg_id 重复（幂等保护，正常丢弃） | 否 |

超时判定权在**平台网关**：到期未收到 response 即记 `E_TIMEOUT` 并触发 STU 的 `fallback_policy`。智能体不得自行判定平台超时。

上表是总线级错误码；研判段（`assess.hazard`）的业务拒绝走 `E_ASSESS_*`，见 §9.4。

## 6. 生命周期

```
启动 → register(能力声明+心跳) → ready → 消费 in → 产出 out → 心跳维持
     → (drain) 平台下发停止 → 优雅退出（不丢在途事务）
```

- `register` payload：`{agent_id, agent_type, capabilities[], hazard_types[], max_concurrency, version}`；
- 心跳周期 ≤5s，丢失 3 个周期 → 平台标记 `unhealthy`、摘除路由、发 `ops.agent.heartbeat_lost`，并按降级策略处理；
- 智能体重启后必须重新注册；平台按 `msg_id` 去重保证重连后重投安全。

## 7. 一致性测试门禁（CI Gate）

真实智能体接入前必须通过 `backend/tests/contract/` 的套件（平台提供，用 Mock 总线驱动）：

1. **Schema 合法性**：所有上行消息 100% 通过 agent_message.v1 校验；
2. **幂等性**：同一 `msg_id` 重复投递不产生重复副作用；
3. **超时语义**：人为延迟响应 > `deadline_ms` 时，平台正确记 `E_TIMEOUT` 并走 fallback；智能体不得在超期后仍触发二次执行；
4. **错误回执**：内部异常必须回 `error.raise` 而非静默；
5. **心跳与注册**：断心跳后平台摘除路由、恢复后重新可用；
6. **数据边界**：静态检查 + 运行时探测，禁止数据库直连；
7. **上下文快照**：`context_snapshot` 能被恢复（人工接管/断点续传演练）。

通过标准：7 项全绿 + 单灾种场景端到端跑通，方可接入生产总线。

## 8. 指标量测点（平台侧自动埋点）

| 指标 | 度量点 | 数据来源 |
|---|---|---|
| 共享同步 ≤3s | `ts` → 下游首次处理时刻 | 网关 span 埋点 |
| 协同成功率 ≥90% | `request→response` 事务成功/总数 | 事务台账表 |
| 调度响应 ≤2s | 节点就绪 → 节点开始执行 | `workflow.*.started` |
| 重调度 ≤10s | 异常发生 → 重调度完成 | `workflow.*.rescheduled` |
| 预警生成 ≤3min | 风险定级结论 → 预警产物 | 链路 trace |
| 触达 ≤20min | 发布 → 通道回执 | `execute.ack` |

指标量测全部落在平台侧，与智能体实现质量解耦，可按 `agent_id` 归因输出给智能体方作为性能反馈。

## 9. 研判契约 `assess.v1`

`assess.hazard` 的 payload 是 `assess.v1` 的 `AssessRequest`，应答是 `AssessResponse`；
平台自建（builtin）、契约门禁对照（mock）与外部实现（`agents_ext`）**共用同一套 DTO**
（Python 镜像 `aegis.domain.assess`，全部 `extra="forbid"`）。平台不认识任何"外部结果形状"。

### 9.1 请求 `AssessRequest`

| 字段 | 必填 | 约束 | 语义 |
|---|---|---|---|
| `contract_version` | 否 | 固定 `assess.v1` | 契约版本 |
| `region_code` | 是 | `^[0-9A-Z]{6,24}$` | 区域编码 |
| `window` | 是 | `^\d{1,4}(m\|h)$` | 证据窗口（如 `60m`） |
| `hazard_type` | assess 侧是 | 平台灾种枚举 | 主灾种（平台按 hits 最高分选定） |
| `candidate_hazards` | 否 | ≤7 | 候选灾种；`hazard_type` 必须属于其中 |
| `hits` | 否 | ≤64 | 规则引擎命中（`TriggerHit`），用于溯源与兜底 |
| `rule_level` | 否 | 1–5 | 规则引擎定级，供实现参考 |
| `intake` | 否 | `telemetry`/`report` | 数据来路 |
| `evidence` | 否 | `AssessEvidence` | **推送腿**：平台构造的窗口证据包 |
| `data_refs` | 否 | ≤32 | 证据快照引用；实现可据此走**拉取腿** |
| `requested_at` | 否 | ISO 时间 | 默认当前时刻 |

### 9.2 证据 `AssessEvidence`

`region_code`、`window`、`generated_at` 必填；`observations`（≤48 条 `FactorObservation`）、
`site_profile`（区域静态画像）、`provenance`（`DataProvenance`）、`unmapped_metrics`（≤24，
显式"不映射"及原因）、`notes`（≤16）、`truncated`。

`FactorObservation` 字段：`factor`（`^[a-z][a-z0-9_]*$`）、`unit`、`samples`（1..100000）、
`min_value`/`max_value`/`mean_value`/`class_value`（数值与类别**至少其一**，数值必须有限，
`min<=max`）、`observed_at`、`station_ids`、`quality`（`ok`/`partial`/`site`）、`sources`、`derivation`。
平台侧按**声明式派生表**生成（见 ADR-0012 §2.1）；无合法派生口径的遥测进 `unmapped_metrics`，禁止近似。

推送/拉取同构：`GET /api/v1/assess/evidence` 返回的对象必须能被 `AssessEvidence.model_validate`
直接解析；实现处理顺序为 **请求内 evidence → `data_refs` 拉取 → 都没有则拒绝**（`E_ASSESS_INSUFFICIENT`）。

### 9.3 响应 `AssessResponse`

| 字段 | 必填 | 约束 | 语义 |
|---|---|---|---|
| `contract_version` | 否 | 固定 `assess.v1` | 契约版本 |
| `hazard_type` / `region_code` | 是 | 枚举 / 区域 pattern | 必须与请求一致 |
| `risk_level` | 是 | 1–5（红/橙/黄/蓝/无） | 统一分级口径 |
| `confidence` | 是 | 0.0–1.0（有限值） | **证据强度**，见 §9.5 |
| `confidence_basis` | 是 | 1–200 字符 | 置信度的可解释依据，空串即拒绝 |
| `rationale` | 否 | ≤1000 | 结论理由 |
| `evidence_refs` | 否 | ≤32 | 结论引用的证据 |
| `assessed_at` / `assessed_by` | 是/是 | ISO / ≤64 | 产出时刻与实现标识 |
| `human_review_suggested` | 否 | bool | 建议人工核签 |
| `double_blind` / `double_blind_mode` | 否 | ≤32 / ≤16 | 双盲（不适用填 `not_applicable`/`""`） |
| `source` | 是 | ≤64 | 实现来源（如 `agents_ext_mock`） |
| `data_provenance` | 否 | `DataProvenance` | 数据出处 |
| `assessment` | 否 | 自由对象 | 实现内部信封（版本/因子/覆盖率等），平台只透传 |

### 9.4 错误码

| 码 | 含义 | 可重试 |
|---|---|---|
| `E_ASSESS_INSUFFICIENT` | 证据/因子不足，拒绝给出等级（**不得**降级成"无风险"） | 否 |
| `E_ASSESS_UNCERTAIN` | 实现内部状态不确定 | 是 |
| `E_ASSESS_FAILED` | 内部执行失败 | 是 |
| `E_ASSESS_HAZARD_UNSUP` | 灾种不在实现支持范围内 | 否 |
| `E_ASSESS_BAD_LEVEL` | 产出的等级超出 1–5 | 否 |
| `E_ASSESS_CONF_MISSING` | 响应缺 `confidence`（含空依据） | 否 |
| `E_ASSESS_BAD_INPUT` | 请求畸形 / region·hazard 与请求不一致 | 否 |

全部错误码满足 AgentMessage schema 的 `^E[A-Z0-9_]{3,20}$`；旧 `E_EXT_*` 已随适配层删除。

### 9.5 置信度语义与数据出处

**`confidence` 是证据强度，不是概率。** 平台只校验范围与 `confidence_basis` 非空，**不代填**——
生产方说不出依据的置信度就是伪证。外部实现用确定性策略自产：
`min(0.60, 0.15 + 0.40×coverage + 0.05×validation)`，`basis` 写明覆盖率、验证结果、方法与封顶值；
MOCK 权重与原型公式封顶 0.60，并恒置 `human_review_suggested=True`。

`DataProvenance`（响应与 verdict/API/台账一等字段）：

| 字段 | 取值 | 语义 |
|---|---|---|
| `data_kind` | `observed`/`derived`/`reported`/`mock`/`demo`/`mixed`/`unknown` | 数据性质；混合含 mock 时 `is_mock=True` |
| `sources` | `platform.telemetry:<source>`、`site_profile:<source>` 等 ≤16 | 出处清单 |
| `method` | `telemetry_rule`/`factor_engine_mock`/`llm`/`report_parse`/`mock_agent`/`unknown` | 产出方法 |
| `method_version` | ≤64 | 方法版本（如 `FACTOR-ENGINE-v0.1`） |
| `is_mock` | bool | 是否含模拟/演示数据 |
| `disclaimer` | ≤500 | 使用限制（如"禁止用于实际预警决策"） |

平台把这些字段随 `RiskVerdict.as_payload()` 写入 API 响应与 `chain_runs.risk`，前端与台账都可见。

### 9.6 灾种键

`hazard_type` 一律使用平台枚举值（`landslide`/`rockfall`/`debris_flow`/`avalanche`/
`lake_outburst`/`quake_triggered`）。外部实现的历史串 `ice_rock_snow_avalanche`、
`glacial_lake_outburst` 仅作为别名接受，归一化到平台值；未知灾种（如 `earthquake`）拒绝。

## 10. 版本演进

- v1 冻结后：**只增可选字段，不改语义**；枚举扩展（灾种、动作）走小版本 + 注册表更新；
- 破坏性变更 → `contracts/agent_message.v2.schema.json` + `agent.<type>.in.v2`，网关支持双栈并行；
- 每次契约变更须同步更新一致性测试套件与本文档，并在 `docs/adr/` 留决策记录。
