# ADR-0003：运行态先内存，持久化后置到 PostgreSQL 17 + PostGIS + pgvector

- 状态：已采纳（2026-10-01 修订：落地实现由 TimescaleDB 改为 PostgreSQL 17 + PostGIS + pgvector）
- 日期：2026-09-29，修订 2026-10-01
- 相关：`storage/store.py`、`persistence/`、`connectors/base.py`、`deploy/postgres/Dockerfile`

## 背景

M1 需要在无外部数据库条件下跑通全链路并做严格的单元/边界测试；而考核指标里"多源数据接入 ≤5min"
"预警准确率回放""空间轨迹查询"最终要落在真实数据库上。

## 决策

1. 先以 `BoundedCollection`（容量上界 + asyncio 锁）承载遥测/预警/任务/链路四类运行态数据。
2. 上层只依赖存储门面（`StoreProtocol.telemetry/warnings/tasks/chains`）与查询参数，不感知底层实现。
3. 持久化阶段引入 **PostgreSQL 17 + PostGIS + pgvector**（asyncpg 直连、无 ORM），
   在同一读视图前挂写路径；连接失败不换实现，而是保留读视图 + 有界写缓冲重试。

### 修订记录（2026-10-01）

原文把持久层写成 TimescaleDB。落地时改为 PostGIS + pgvector，理由是判定口径变了：

- 本 ADR 要解决的是**空间轨迹查询**（站点半径/多边形内的读数与预警）与**向量召回**（历史 STU），
  对应扩展是 `postgis` 与 `vector`；时序压缩/连续聚合（Timescale 的强项）不在指标里。
- `persistence/sql/001_extensions.sql` 只 `CREATE EXTENSION postgis, vector`，
  而 `timescale/timescaledb:2.17.2-pg16` 镜像不含 pgvector —— 按原方案部署会在建扩展时直接失败。
- 因此 compose 改用 `deploy/postgres/Dockerfile`：`postgis/postgis:17-3.5` 之上装
  `postgresql-17-pgvector`（本机对镜像实测：Candidate = 0.8.6-1.pgdg11+1，PGDG 源自带）。

## 后果

- 正面：测试零外部依赖、可离线全绿；开发期不需要维护数据库迁移；长跑内存有上界不会 OOM；
  空间与向量能力由同一实例提供，省掉一套向量库。
- 代价：进程重启丢运行态数据（选 `memory` 后端时）；没有 Timescale 的连续聚合，
  分钟级物化改由分析旁路承担（ClickHouse 的 AggregatingMergeTree 物化视图 / DuckDB 边缘单文件，见 ADR-0005）。
- 对外可见性：`store_backend` 与实际驱动、是否降级，一律经 `GET /api/v1/integrations` 报出；
  缺 DSN 属配置错误，容器构造期即失败，不静默退回内存视图。

## 备选方案

- TimescaleDB：时序压缩与保留策略成熟 —— 但本指标不需要压缩，且镜像缺 pgvector，否决。
- 用 SQLite 承担时序写入：写吞吐不足、无空间与向量扩展 —— 否决（`db_url` 配置项已随本次修订删除）。
- 独立向量库（Qdrant/Milvus）：向量检索能力更强 —— 但多一个服务与一套凭据，
  在 16 条案例 + 万级 STU 的规模下收益不成立，暂缓。
