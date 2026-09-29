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

已完成（M1）：契约、总线与网关、能力注册、规则/定级/拆解/预警/触达、降级链路、运行态存储、HTTP API、
Web 前端四页（含 SSE 实时事件）、单元/契约/端到端/API/性能五类测试、容器编排与 CI。

进行中或未开始（诚实标注）：
- 柔性可视化工作流引擎与 Vue Flow 画布（M2，指标 2 的完整口径；当前为固定流水线 + 降级双轨）
- Neo4j 灾情知识图谱与 GraphRAG 检索（复用 NexusMind `graph_builder`/`vector_store` 资产，M2）
- TimescaleDB 持久化替换内存存储（接口已解耦）
- GIS 三维一张图（Cesium）与风险网格真实行政区划叠加
- 真实通道（短信/北斗/广播）对接与边缘弱网演练
