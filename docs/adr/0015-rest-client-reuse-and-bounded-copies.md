# ADR-0015：HTTP 客户端复用与"便捷副本"有界化——把每请求的开销从链路挪到进程

- 状态：已采纳（2026-11-10）
- 日期：2026-11-10
- 相关：ADR-0014（后端热点消除，本 ADR 是其延续）、
  `docs/architecture.md` §4（性能设计）、`docs/REPORT.md`（本 ADR 的数字都有原始取证）
- 涉及代码：`backend/src/aegis/agents/rest_context.py`、`agents/assess.py`、
  `pipeline/stages.py`

## 背景

ADR-0014 处理的是"**每次要一个汇总数，就把整份底表扫一遍**"。本轮处理的是另外两处
同源的形状：**每次要做一次事，就把一份可复用的东西重建一遍**。

### 1. REST 取数门面每次请求新建 HTTP 客户端

`RestAssessContext._get()` 原本长这样：

```python
async with self._client_factory(timeout=..., transport=..., headers=...) as client:
    response = await client.get(...)
```

一次 `corroborate()` 发两个请求（案例 + 检索），于是**每条链路建两个客户端**。

量到的根因不是网络，是 `httpx.AsyncClient` 的**构造**：

```
_httpx._transports.default.HTTPTransport.__init__
  → ssl.SSLContext.load_verify_locations     ← 从磁盘重载 CA 证书包
```

实测 **11.1 ms/次**纯 CPU（`_ssl._SSLContext.load_verify_locations` 占 0.319s/30 次）。
更糟的是它**同步阻塞事件循环**：20 次构造期间，一个 5 ms 周期的业务协程被推迟了
**304 ms**。

一次 `corroborate()` = 2 个客户端 = 22 ms 的事件循环卡顿，**每次研判都付**。

### 2. `HazardStages._warnings` 是一份无界的"便捷副本"

`stages.py` 里 `self._warnings[warning_id] = dispatched` 记下本进程发布过的每条预警，
**从不淘汰**。每条 `WarningRecord` 约 4 KB，十万条就是 150 MB。

而预警的**权威读视图**是 `store.warnings`（`WarningStore`，ADR-0014 已给它加上界）。
这份 dict 只是 `warnings` 属性（`ChainStations.warnings` → 对外形状的一部分）用的便捷副本。

同文件里就有正确写法的样板：`_agent_hits` 用 `deque(maxlen=agent_hit_window)` 有界。

## 决策

### 1. HTTP 客户端复用（`rest_context.py`）

`RestAssessContext` 持有一个**懒建、复用**的 client：

- 首次调用才建（付一次 11 ms），之后每次调用零成本；
- 用锁串行化首次创建——并发研判时不该有两个线程各建一个；
- **令牌带在请求上而不是建在 client 上**：client 是复用的，令牌可能换，
  换令牌不该逼着重建 client；
- 新增 `aclose()`：**谁建谁关**（与 `connectors/weather_api.py` 同一口径），
  由 `AssessAgent.stop()` 调用（智能体持有门面，停机时关门面）。

**为什么与 MCP 门面的做法不同**：MCP 那边是**协议级会话**
（connect / initialize / call / close），开合是协议要求，那边 docstring 也明确写了
"不做跨请求长连：长连要管重连、管并发、管进程停服时的在途调用"。
REST 这边是纯 HTTP，keep-alive 本来就该用——MCP 的顾虑（会话状态、重连语义）
在这里不存在。

### 2. 便捷副本有界化（`stages.py`）

`self._warnings` 加窗口（默认 5000，与 `WarningStore` 同量级），超出时按插入序丢最旧的。
同 `warning_id` 覆盖时长度不变，不会误删。

**不改成"直接读 store"**：那是跨层的依赖改道（pipeline → storage 已有先例但要动装配），
而这份 dict 的定位本来就是"便捷副本"——把它有界化，问题就解决了。

## 后果

**好的**：

- `corroborate()`：**28.3 → 2.0 ms/次（14×）**；
- 事件循环卡顿：20 次 `corroborate` 期间，5 ms 周期协程最大被推迟 **304 → 13.4 ms**；
- `stages._warnings` 从无界变成有界（默认 5000 条 ≈ 20 MB 上限，此前无上限）；
- 复用不改变对外形状：`corroborate()` 的返回值、降级原因、请求行与查询参数逐字不变
  （既有 21 条 REST 用例全绿）。

**要付的代价**：

- 多一个要关的东西：门面持有连接池，必须在智能体停机时关掉。
  漏关的后果是连接池泄漏——所以用例钉住了"停机必须关门面的客户端"。
- `HazardStages` 多一个构造参数 `warnings_window`（默认 5000）。

**明确不做**：

- 不给 MCP 门面改成长连（那边是协议级会话，且 docstring 已记明理由）；
- 不把 `stages._warnings` 改成直接读 `store.warnings`（要动装配与分层）；
- 不做 HTTP 客户端的自动重建（连接真的坏掉时 httpx 自己会重连，
  实测 `corroborate()` 在服务不可达时仍按原口径返回降级原因，不是抛异常）。
