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

| 动作 | 发起方 | 方向 | payload 必填字段 | 说明 |
|---|---|---|---|---|
| `perceive.anomaly` | perceive | out→event | `station_id`,`metric`,`value`,`observed_at`,`anomaly_score` | 弱信号/越限标注上报 |
| `perceive.trigger_hit` | perceive | out→event | `rule_id`,`hazard_type`,`region_code`,`evidence_refs` | 触发条件命中（指标1 溯源依据） |
| `assess.hazard` | platform→perceive/assess | in→request | `region_code`,`window`,`candidate_hazards` | 请求灾种识别 |
| `assess.risk_level` | assess | out→response | `hazard_type`,`region_code`,`risk_level`(1-5),`confidence`,`rationale`,`evidence_refs` | 定级结论 |
| `plan.stu` | platform→plan | in→request | `event_id`,`risk`,`objective`,`constraints` | 请求任务拆解 |
| `plan.stu_result` | plan | out→response | `task_units`(STU 数组，须过 stu.v1 校验) | 返回标准化任务单元集 |
| `plan.reschedule` | platform→plan | in→request | `workflow_instance_id`,`anomaly`,`current_topology` | 异常工况重调度建议（≤10s） |
| `execute.warn` | platform→execute | in→request | `warning_id`,`level`,`regions`,`audiences`,`content_refs` | 预警发布指令 |
| `execute.ack` | execute | out→response | `warning_id`,`channel_results`(每通道 delivered/pending/failed) | 发布结果回执 |
| `feedback.status` | feedback | out→event | `warning_id`,`reach_stats`,`response_state`,`effect_score` | 响应/触达/效果反馈 |
| `error.raise` | 双向 | out→error | `code`,`message`,`retryable` | 失败回执（见 §5） |

**风险等级统一口径**：`risk_level` 1-5 对应红/橙/黄/蓝/无（1 最高），所有智能体与平台共用，禁止自定义分级。

## 4. 数据访问边界

智能体取数只能走平台数据 API（只读，服务令牌鉴权）：

- `GET /api/v1/telemetry` — 时序查询（站点、指标、时间窗）
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

## 9. 版本演进

- v1 冻结后：**只增可选字段，不改语义**；枚举扩展（灾种、动作）走小版本 + 注册表更新；
- 破坏性变更 → `contracts/agent_message.v2.schema.json` + `agent.<type>.in.v2`，网关支持双栈并行；
- 每次契约变更须同步更新一致性测试套件与本文档，并在 `docs/adr/` 留决策记录。
