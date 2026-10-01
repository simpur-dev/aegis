# AEGIS 平台架构说明（实现态）

> 本文只描述**已经在仓库里运行的代码**。规划中但未实现的部分显式标注状态，
> 避免文档领先于实现。权威目标架构见《课题6_项目架构设计》与根目录方案文档。

## 1. 分层与代码位置

| 层 | 职责 | 代码 |
| --- | --- | --- |
| L1 感知接入 | 多源采集、协议适配、时空归一、质量标记 | `backend/src/aegis/connectors/` |
| L2 数据层 | 遥测/预警/任务/链路运行态存储；图谱与向量检索（M2 接入 Neo4j+Graphiti） | `backend/src/aegis/storage/` |
| L3 智能层 | 五大智能体（**外部实现**，经契约接入） | `contracts/`、`backend/src/aegis/agents/`（Mock 参考） |
| L4 服务层 | 触发规则、定级、任务拆解、预警生成、靶向触达、链路编排、HTTP API | `services/`、`pipeline/`、`api/` |
| L5 应用层 | 纯 Web：态势总览 / 监测预警 / 预警发布 / 指标量测（Vue3 + Vite + TS + AntD + ECharts） | `frontend/src/views/` |
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
  ⑤ feedback  : 汇总触达回执 → ops.feedback.status（反馈智能体可订阅）
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
- **知识与检索**：Graphiti 双时态案例图谱（读写分离，LLM 只在写路径）、
  bge-m3 + bge-reranker int8/CPU 混合检索（dense + BM25 + RRF，LLM 不进检索回路），
  两者都接进预案生成链路并对外暴露只读接口。
- **分析旁路**：ClickHouse 分钟级物化与 DuckDB 边缘单文件离线分析（write-behind，热路径不等 OLAP）。
- **可观测**：OpenTelemetry 链路 + 自研时延账本 + Prometheus/Alertmanager 规则；
  `GET /api/v1/integrations` 把每条可选腿的启用/降级事实对外报出。
- **一张图**：Cesium + 自建 quantized-mesh 地形 + PMTiles 离线底图（不依赖 Ion/谷歌），
  含离线与底图守卫的前端测试。

未完成或待取证（诚实标注）：

- 真库在线取证：PostGIS/pgvector 的容器内 DDL + 空间/向量查询、ClickHouse 的 DDL + 物化视图、
  Jaeger 按 trace_id 的端到端找回与 `promtool check rules` —— 代码与规则齐备，一次真实跑证还没做。
- 真实通道对接（短信/北斗/广播）与边缘弱网实链路演练：目前分别是 mock 通道与本机 Zenoh POC。
- Zenoh 目前是站端↔网关链路的 POC，平台侧总线仍是 NATS JetStream（未替换）。
- MQTT 字段设备接入：`connectors` 里没有 MQTT 数据源，配置里的 `mqtt_*` 与 compose 的 EMQX
  属于"已声明未实现"，实现或移除二选一（见任务清单）。

复现命令与数字口径统一收录在 `docs/REPORT.md`。
