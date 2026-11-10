# ADR-0014：后端观测与存储链路的热点消除——把"每次都全扫一遍"改成"一次算完"

- 状态：已采纳（2026-11-10）
- 日期：2026-11-10
- 相关：ADR-0003（内存存储先行）、ADR-0005（性能栈）、
  `docs/architecture.md` §4（性能设计）、`docs/REPORT.md`（本 ADR 的数字都有原始取证）
- 涉及代码：`backend/src/aegis/observability/tracer.py`、`observability/metrics.py`、
  `bus/gateway.py`、`storage/store.py`、`services/llm/llm_gateway.py`、`container.py`

## 背景

平台自身的观测与存储链路（不是外部实现那一侧）有四处**同一形状的缺陷**：
每次要一个汇总数，就把整份底表扫一遍。底表随运行时长增长，扫描成本随之线性放大，
而这些出口的调用频率都很高（Prometheus 每 10 秒抓一次、指标页每次打开算一次）。

四处都是先**量**出来的（不是读代码猜的）：

| 位置 | 形状 | 实测 |
| --- | --- | --- |
| `LatencyLedger.snapshot()` | 每个指标名各扫一遍全账本 | 12 指标名 × 20 万样本 = **64 ms** |
| `LatencyLedger.iterate_since()` | 每次增量导出全量扫账本 | 20 万样本 = **7.4 ms/次**，Prometheus 每 10 秒一次 |
| `MetricsExporter.collect()` | 为拿"在线智能体数"这一个整数去算**整份**网关快照（含时延账本全量统计） | 同上 64 ms |
| `WarningStore` / `TaskStore` | 旁路索引**从不淘汰** | put 2 万条（上限 5000）→ 索引留 **2 万条** |
| `HandlingTaskStore.list_spaces()` | 每个空间全扫一遍任务表（N+1） | 200 空间 × 2 万任务 = **26 ms** |
| `LlmGateway.first_pass_stats()` | 每个 task 全表过滤一遍 | 12 task × 4 万行 = **20.8 ms** |

两处代码与自己的注释/文档**不一致**，这比"慢"更值得记：
`tasks.py` 的 `list_spaces` 写着"内存侧同语义，**不做 N+1**"，而内存侧恰恰在做 N+1；
`WarningStore` 的 `get()` 与 `list()` 对"哪些预警还在"给出**两种答案**
（列表已淘汰最旧的，`get()` 却还能捞到它）。

## 决策

### 1. 时延账本聚合：一次遍历分桶（`tracer.py`）

`snapshot()` 改成走新的 `stats_all()`：**一次**遍历把样本按指标名分桶，
再逐桶算统计量。成本从 O(指标名数 × 样本数) 降到 O(样本数)。

分位数、违约计数、单位、`breaches` 的出场条件（没登记预算就不带这三格）
全部按原口径保留——`stats()` 与 `stats_all()` 走**同一个** `_stats_of()`，
避免两条读面给出不同的数。

### 2. 增量导出：只走新增的那一段（`tracer.py`）

`iterate_since()` 改为**从尾部反向走、撞到边界就停**。
依据：账本的 `seq` 严格单调递增（`record()` 单调发号），deque 按写入顺序存放，
所以"seq > index 的样本"必然是**尾部一段**。成本从"账本全长"降到"新增条数"。

### 3. Prometheus 导出：不再为拿一个整数算整份快照（`metrics.py` + `gateway.py`）

导出器只需要"在线智能体数"，却调了 `gateway.snapshot()`——那份快照含时延账本全量统计。
给网关单开一个只读出口 `online_count`，导出器改用它。

**为什么不改 `snapshot()` 的形状**：那份快照是对外契约（`/api/v1/...` 与测试都依赖它）。
加一个窄出口比改已有出口的语义安全。

### 4. 存储的旁路索引：与主集合同步收缩（`store.py`）

- `WarningStore`：顺序队列改为**无界 deque + 显式淘汰**，
  淘汰时索引同步去掉。有界 deque 挤条目是**静默**的，索引等不到通知，
  于是索引无界增长、`get()` 与 `list()` 口径不一致。
- `TaskStore`：给 `BoundedCollection` 加 `on_evict` 回调。
  **淘汰通知放在 `BoundedCollection` 里，是因为只有那一处知道上限是多少**；
  同 id 被写过多遍时，索引指向最新那份，淘汰最旧那份**不能**误删索引
  （故按对象身份判定）。

### 5. 空间计数与 LLM 用量：一次归拢（`store.py` + `llm_gateway.py`）

- `list_spaces()`：一次遍历 `_rows` 归拢出各空间的任务总数与未闭数，
  与 SQL 侧的 `GROUP BY` 同形。
- `first_pass_stats()`：一次遍历把台账行按 task 归拢，再逐 task 算首过率。
  口径不变：只有带结构化调用的 task 才出现在 `by_task` 里，
  每个 task 的分母仍只算"模型真的答了"的行。

## 后果

**好的**：

- 时延账本聚合：**64 ms → 22.5 ms**（2.7×，20 万样本 / 12 指标名）；
- 增量导出：**8.9 ms → 0.0 ms**（同规模，成本从"账本全长"降到"新增条数"）；
- Prometheus 抓取不再触发时延账本的全量统计（每 10 秒一次）；
- 空间计数：**25.6 ms → 1.0 ms**（26×，200 空间 × 2 万任务），计数口径逐项一致；
- LLM 首过率逐 task 拆分：**20.8 ms → 8.9 ms**（2.3×），逐 task 结果一致；
- 两个存储的索引不再无界增长（2 万条 → 5000 条），且 `get()` 与 `list()` 口径一致；
- 新增用例 32 条，其中**等价性**用例把旧算法独立重写一遍当基准、逐项比对输出。

**要付的代价**：

- `WarningStore.get()` 对**已被淘汰**的预警从此返回 `None`
  （此前索引留着它，能捞到一条列表里已经不存在的预警）。
  这是把"索引与列表对同一批记录给出不同答案"这个不一致**修掉**，
  代价是极老预警的单独查询不再命中（内存形态；Postgres 形态不受影响）。
- `TaskStore.get()` 同理。

**明确不做**：

- 不给账本加"统计结果缓存"（分位数随新样本变化，缓存的失效口径要另立一套；
  而分桶后 22.5 ms 已不是瓶颈）；
- 不改 `snapshot()` 的对外形状（那是对外契约）；
- 不动 `AlertStore` / `ControlActionStore` / `NotificationStore`——
  它们**已有**配套淘汰逻辑（`store.py` 里 `while len(self._rows) > ...` 那几处），
  本批只修缺这一段的两处。
