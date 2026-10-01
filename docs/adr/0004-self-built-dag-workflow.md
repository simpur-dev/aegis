# ADR-0004：工作流引擎自研 DAG + 状态机

- 状态：已采纳（M2 已落地并实测）
- 日期：2026-09-29，修订 2026-10-01
- 相关：《课题6_项目架构设计》§4.2、`contracts/stu.v1.schema.json`、`backend/src/aegis/workflow/`

## 背景

指标要求"≥10 类防控任务节点自定义配置、常规调度响应 ≤2s、异常工况识别与重调度 ≤10s"，
并要求可视化拖拽编排与**运行中动态改图**。M1 的 `HazardResponseChain` 是固定流水线，
其各段与未来的工作流节点一一对应（STU 已带 `sla_seconds / required_capabilities / fallback_policy`）。

## 决策

1. 自研轻量 **DAG + 状态机**引擎：`WorkflowDef（带版本）/ WorkflowInstance / NodeRun / Edge`。
2. 节点类型注册机制，画布与引擎共用同一份注册表。
3. 调度队列复用 NATS JetStream，就绪节点事件驱动出队，避免轮询。
4. "柔性"体现为三点：模板与实例版本分离；运行中替换参数/插入节点/旁路节点；异常三段处置（重试/转移/升级）。
5. 画布采用 Vue Flow（MIT），语义取 BPMN 2.0 子集（网关/任务/事件），不实现完整 BPMN 规范。

## 落地事实与实测（2026-10-01，本机）

- 注册节点类型 **16** 类（口径：`WorkflowEngine.node_types`，容器启动后实测）：
  `api_call, branch, data_fetch, degrade_to_rule, delay, device_control, feedback_collect,`
  `hazard_identify, human_review, join, notify, risk_assess, situation_simulate, threshold,`
  `warning_generate, warning_publish` —— 满足"≥10 类"。
- 时延为 `scripts/metrics_report.py --agents --rounds 6`（seed 202609，17 事件、51 事务样本）的实测值：
  - 常规任务调度响应：P50 30.5ms / P95 40.2ms，阈值 2000ms，越限 0；
  - 异常工况识别与重调度（事务口径）：P50 28.9ms / P95 36.5ms / 最大 37.6ms，阈值 10000ms，越限 0；
  - 多节点数据共享同步：P50 3.0ms / P95 6.0ms，阈值 3000ms，越限 0。
- 回归护栏：`tests/unit/test_workflow_engine.py`、`test_workflow_model.py`、`test_workflow_properties.py`
  （hypothesis 性质测试覆盖"无环、就绪集单调、旁路不失联"等结构不变量）。

## 备选方案对照（调研结论）

选型的判定口径不是"功能强弱"，而是三条硬约束：**边缘自治（无中心服务也能跑）**、
**运行中改图是产品命题而不是二开**、**许可证与依赖面能进赛题交付**。

| 方案 | 许可 | 运行时依赖 | 运行中改实例 | 判定 |
| --- | --- | --- | --- | --- |
| Conductor OSS | Apache-2.0 | Java server + PostgreSQL + Redis/Dynomite | 有真·运行时改实例 API：`skipTaskFromWorkflow`、`PUT /api/workflow/{wfId}/{taskRef}/{status}` | **不引入**：三件套中心服务直接破坏边缘自治；其运行时改图 API 作为本引擎 API 设计的对照样例引用 |
| Temporal | MIT（服务端） | 需持久库（Cassandra/MySQL/PostgreSQL）+ worker 进程；工作代码要满足确定性重放 | 通过版本化 workflow 与 update/signal 实现，改图需发新版本 | 不引入：确定性重放模型与"边跑边改节点"的产品命题相反，运维面在高原站点过重 |
| Camunda 7（嵌入式） | Apache-2.0（社区版） | JVM 嵌入式引擎 | BPMN 实例可迁移，但自定义节点仍需二次开发 | 不引入：Java 栈与本平台（Python/asyncio）不同构 |
| Camunda 8 / Zeebe | 非 OSI 开源许可（具体条款待核对，不在交付中作确定性表述） | 需独立 broker + 附加服务 | Operate/自 API 侧操作实例 | 不引入：许可与运维形态都不满足赛题交付约束 |
| Prefect | Apache-2.0 | server + 数据库，面向数据管道调度 | 以部署/版本切换为主 | 不引入：调度粒度与"人在环、事件驱动重调度"不匹配 |

上表中除 Conductor 的两条 API 名称（来自其公开 OpenAPI，且已写入本引擎的对照注释）外，
其余各家许可证与依赖形态均属**调研期结论**，正式对外引用前需按官方 LICENSE 与文档逐条复核——
本 ADR 不把这些写成可第三方引用的事实。

## 后果

- 正面：动态重编排是专利与论文的核心命题，自研可控、可插桩（≤2s/≤10s 埋点直达引擎内部），
  依赖面小，弱网边缘节点也能跑；上表的实测数字全部来自本引擎，不含外部服务。
- 代价：需自建调度器与可视化画布，工作量最大；由 e2e 压测（节点数梯度）持续守住时延预算；
  失去成熟引擎的重试/补偿/可视化运维生态，这些能力必须在自有埋点与状态机里补齐。
