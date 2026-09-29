# ADR-0003：运行态先内存、持久化后置到 TimescaleDB

- 状态：已采纳
- 日期：2026-09-29
- 相关：`storage/store.py`、`connectors/base.py`

## 背景

M1 需要在无外部数据库条件下跑通全链路并做严格的单元/边界测试；而考核指标里"多源数据接入 ≤5min"
最终要落在真实时序库上。

## 决策

1. 先以 `BoundedCollection`（容量上界 + asyncio 锁）承载遥测/预警/任务/链路四类运行态数据。
2. 上层只依赖存储门面（`PlatformStore.telemetry/warnings/tasks`）与查询参数，不感知底层实现。
3. M2 引入 TimescaleDB（与 PostgreSQL 同栈）替换实现，同时补数据血缘与保留策略。

## 后果

- 正面：测试零外部依赖、可离线全绿；开发期不需要维护数据库迁移；长跑内存有上界不会 OOM。
- 代价：进程重启丢运行态数据；因此本 ADR 明确把"持久化 + 保留策略"列为 M2 必做项，
  并在 CI 报告中标注当前指标测量基于内存运行态（`scripts/metrics_report.py` 的运行参数段可见）。

## 备选方案

- 一开始就接 PostgreSQL/Timescale：需要容器编排才能跑测试，单测门槛显著抬高 —— 暂缓。
- 用 SQLite 承担时序写入：写吞吐与时序压缩能力不足 —— 否决。
