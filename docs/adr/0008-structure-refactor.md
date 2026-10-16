# ADR-0008：结构重构 P1–P6——模块边界与分层归位

- 状态：已采纳（2026-10-16 起分六笔落地，每笔独立提交且门禁全绿）
- 日期：2026-10-16
- 相关：ADR-0001（契约优先，本 ADR 强化其方向性）、ADR-0002（双轨机制不动）、
  ADR-0004（自研 DAG 不动）、ADR-0006/0007（其"相关"路径指针随本次搬家更新）、
  `backend/tests/unit/test_architecture_rules.py`（铁律的机器执行处）、`docs/architecture.md` §1 分层表

## 背景

用户反馈"项目非常乱、架构不清晰"，并把痛点定在**代码结构：模块边界与分层**。审计后的实测结论是：
问题不是依赖成环（包级方向基本正确，`domain` 零依赖这条铁律真的成立），而是四类可数的形状缺陷——

1. **四条反向依赖**：`storage/store.py`（运行时）与 `persistence/{rows,postgres}.py`（TYPE_CHECKING）
   import `pipeline.result`；`services/{task_parser,quality_eval}.py` import `bus.gateway` 只为拿契约校验器。
   持久层够应用层、服务层够总线层，方向都是反的。
2. **两个上帝文件**：`integrations.py` 1059 行 / 8 个 `build_*`；`container.py` 1174 行 / 68 字段 / 31 方法
   （业务判定住在装配点里）。
3. **一个巨函数 API**：`api/app.py` 1125 行，37 条路由全挤在 `create_app` 体内。
4. **一个 21 模块的平铺大包**：`services/` 无子结构。

目标只有一句：**打开一个包就知道谁管什么**，且行为零变化。

## 决策

1. **依赖方向优先于目录美观（P1）**。`RiskVerdict`（纯 dataclass）与 `pipeline/result.py` 整体下沉为
   `domain/verdict.py` 与 `domain/chain.py`，于是 `domain` 的定位从"枚举与契约镜像"扩为
   **"枚举、契约镜像、链路产物与定级"**——它是对外口径的唯一真源，api、前端、`chain_runs` 落库都读它。
   **`ContractRegistry` 不进 `domain`，归顶层 `aegis/contracts.py`**：铁律 4 禁止 `domain` import 任何
   `aegis.*`，而它要抛 `aegis.errors.SchemaInvalidError`。这条约束是硬的，宁可让顶层多一个零依赖模块
   （`errors.py`、`config.py` 已是先例），也不给铁律开例外。
2. **修完就锁死（P1）**。反向依赖修掉后立刻加 AST 守卫：`storage`/`persistence` 不得 import `pipeline`、
   `services` 不得 import `bus`。理由是这类边会自己长回来——"顺手 import 一下更方便"是它当初的成因。
3. **装配身份按路径认，不按文件名认（P2/P4）**。`integrations.py` 拆成包后，"只有装配点能调腿的 `build_*`"
   这条纪律属于整个包；白名单因此从文件名集合改成路径匹配，并**给白名单自己加自检**
   （装配面扫不到任何文件就红）。否则目录改名会把守卫静默变成"谁都不许调用"或永绿。
   同一批偏离计划的地方也记在这：`AssembledEvidence`/`AssembledScenarios`/`sandbox_runner` 进了
   `integrations/`（它们折的是可选腿的形状，属装配层），而不是计划写的 `pipeline/report_intake.py`。
4. **API 按资源拆，鉴权不在这一层（P3）**。`api/app.py` 只留装配（闸 / lifespan / 异常收口 / MCP 挂载 / include），
   端点进 `api/routers/{ops,data,knowledge,rules,drill,warnings,agents,memory,stream}.py`；
   拆法按"读哪一块平台状态"而不是按 HTTP 方法。共用依赖收进 `api/deps.py`，
   顺带消灭 `get_container` 的三份逐字重复实现。
5. **守卫的取表口径改从对外契约面读（P3，本 ADR 最重要的一条）**。FastAPI 0.141 的 `include_router`
   在路由表里留的是 `_IncludedRouter` 包装，端点藏在 `original_router.routes`；
   因此"遍历 `app.routes` 数业务路径"的门禁从 51 条静默数成 15 条——**覆盖面缩水而表现是全绿**。
   判据改为读 `app.openapi()["paths"]`（客户端看见的那张表）。这是"按实现细节比对的守卫会在搬家后骗人"
   的一个具体形态，写进本 ADR 是为了让它可复用。
6. **业务腿不留委托方法（P4）**。人工上报整体搬成 `pipeline/report_intake.py` 的 `ReportIntake`，
   容器只留一个**必填**字段 `report_intake`，不再有 `container.submit_report`。
   两处都能调的入口迟早长出第二套口径，而"助手页上报"与"表单上报"必须同源；
   必填让装配遗漏在构造期就炸，而不是等有人点上报才发现。`report_stats` 由装配点建一次、
   容器与受理腿拿同一个 dict 引用（一本账只有一个出处）。
7. **分组只买可发现性，不买依赖收益（P5）**。`services/` 21 个平铺模块按实测耦合方向分五组
   （`rules/`、`llm/`、`interpret/`、`response/`、`memory/`），`simulation.py` 保持平铺（与顶层
   `aegis/simulation/` 同名异层，再套一层只会更绕）。诚实声明：组间 import 仍然允许
   （`interpret/semantic_parser` → `rules/risk_engine` 本就跨组），所以这一步**不产生新的方向收益**。
   `memory` 包与 `memory.py` 模块同词，得到 `aegis.services.memory.memory`——重复但真，不改名。
8. **全程不留转发 shim**。旧路径继续可 import 等于"模块归谁"由谁先 import 决定，与本次要消灭的东西同源；
   "任何时刻可回退"由"每阶段改完立刻跑整棵门禁 + 独立提交"保证，不由 shim 保证。

## 后果（可核对的数字）

`BEFORE` 取 P1 之前那一笔 `fa2c348`，`AFTER` 取当前工作树，脚本实测：

| 面 | 前 | 后 | 最大单文件（后） |
| --- | --- | --- | --- |
| 装配中心 | 1059 行 / 1 文件 | 1442 行 / 14 文件 | `integrations/mcp.py` 285 |
| HTTP API 层 | 1611 行 / 4 文件（`app.py` 独占 1125） | 1802 行 / 15 文件 | `api/routers/rules.py` 206（未动的 `workflow_api.py` 303） |
| 装配容器 | 1174 行 | 803 行 | — |
| 服务层 | 5284 行 / 21 模块平铺 | 5279 行 / 5 子包 + 1 平铺 | `services/interpret/assistant.py` 955（未改内容） |
| 领域内核 | 663 行 | 807 行 | `domain/messages.py` 446 |
| `backend/src/aegis` 合计 | 130 文件 / 32286 行 | 162 文件 / 32744 行 | — |

行数的净增（+458）全部是每个新模块的头部说明与各自 import——搬运用 AST/符号集合核对，不看行数：
P2 比对新旧两侧顶层定义 45 个函数/类 + 5 个常量，丢失 0、新增 0；P3 比对 59 个定义的 AST dump
（剥掉装饰器名）等式成立，另比 `openapi()` 摊平后的 1539 个叶子逐项相等、54 条路径集合相同。

代价与风险：包与文件变多，跨文件跳转比从前多一次；"读源码文本"的守卫共 6+ 处的路径跟着搬家改
（前后端都有），漏改一处就是静默永绿——所以每处都配了变异检查（P1–P5 共 15 条，全部 `rc==1` 且红≥1）。
另有两处坑值得记住：Python `write_text` 在 Windows 上把 LF 写成 CRLF，会改坏按文本比对的前端守卫
（仓库 `.gitattributes` 是 `* text=auto eol=lf`）；codemod 里旧路径是新路径前缀时会二次命中，
产出 `aegis.services.memory.memory.label_flywheel` 这种废路径——它在收集阶段就红，不在"绿了再提交"之后。

## 被拒的备选

- **旧位置留一行 re-export shim**：见决策 8。
- **请三方依赖检查器（import-linter 一类）**：铁律已经由 200 行 AST 守卫执行且能在 CI 前本地跑，
  再引一个工具会带来"工具配置与守卫测试各说各话"的第二口径。
- **API 保持 `register(app)` 闭包形态**：能保住 `app.routes` 平铺，但把 37 条路由继续锁在一个函数作用域里，
  与本阶段目标相反；路由表取表口径改从契约面读是更小的代价。
- **把 `ContractRegistry` 塞进 `domain` 并给铁律 4 开个例外**：例外一开，"domain 零依赖"就从
  一条可机器判定的纪律退化成一句注释。
