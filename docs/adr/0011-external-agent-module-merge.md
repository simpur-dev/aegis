# ADR-0011：外部交付智能体模块的并入方式——适配层接入、两套并存、原始物料存档

- 状态：已采纳（2026-11-09）
- 日期：2026-11-09
- 相关：ADR-0001（契约先行边界）、ADR-0002（智能体优先平台降级）、ADR-0007（LLM 智能体化升级）、
  ADR-0008（模块边界与 AST 守卫的做法）、`contracts/AGENT_INTEGRATION_SPEC.v1.md`、
  `docs/architecture.md` §3.1、`archive/external_agents_source_20261109/README.md`

## 背景

一份外部交付的智能体模块（`hazard-landslide-agent`，同学的 LangGraph 多智能体实现，
六个 agent：data / feedback / manager / risk / simulation / warning，原仓库约 16.7k 行、
109 条用例；并入主线后是 `backend/src/aegis/agents_ext/` 下 132 个 Python 文件、约 15k 行）
要并入本仓。它与本仓自建内环有**两种模型上的根本差异**：

| | 外部模块 | 本仓平台 |
| --- | --- | --- |
| 运行形态 | 单进程 LangGraph 图编排，进程内函数调用 | 分布式总线，智能体是总线上的独立服务 |
| 对外接口 | `run_xxx_agent(...) -> dict` | `AgentMessage v1`（唯一交互载体） |
| 依赖 | langgraph / langchain（自带） | 平台既有依赖 |
| 数据面 | 内存 Mock 源（其实现自述 "MOCK-v0.1"） | 平台真实数据面（数据 API / MCP / 共享记忆） |

直接拼接会两头坏：把她的代码直接并进平台自建内环（`aegis.agents`），会让两份**形态根本不同**的
实现糊在一起（她那套是单进程图编排、进程内函数调用，不是总线上的独立服务形态）；
不搬而只写文档则等于没并入。同时它**带着两个必须处理的问题**：
① 原仓库把**明文 API Key 写死在源码里**并提交进了公开仓库；② 它的运行时产物
（SQLite checkpoint、反馈记忆 JSON）会写进代码目录。

本 ADR 记的是**怎么并**，不是"要不要并"——那是用户已拍板的前提。

## 决策

### 1. 落位：`backend/src/aegis/agents_ext/`，在 `aegis` 包内

**为什么不在仓库根的 `agents/`**：那个目录不在包路径上，代码不会进 wheel、不进镜像，
部署时就没有它。它保留为**交付落点的索引**（README + 状态表），实现另置。

**依赖独立成可选 extra `agents_ext`**：langgraph / langchain-core / langchain-openai /
langgraph-checkpoint-sqlite。没装这层时平台**一律退回自建内环**并记 warning——
一个可选依赖不该决定整条链路能不能跑。镜像里也装上它：不是为了默认启用，
而是为了让"切实现"在容器形态下也做得到（依赖不在镜像里时，部署方想选 ext 就得自己改镜像构建）。

### 2. 接口统一：三层分工，把"契约壳"与"语义翻译"分开

平台的 `AgentMessage` 请求 → 她的入口参数，她的返回 dict → `Reply(payload=...)`——
这条翻译**不放在一个文件里**，拆成三层：

| 层 | 文件 | 职责 | 跟着什么变 |
| --- | --- | --- | --- |
| 契约壳 | `agents_ext/adapter.py` | `AgentKit.handle` 样板（订阅、心跳、幂等、deadline、错误回执）+ 动作分派 | 跟平台契约走 |
| 语义翻译 | `agents_ext/contract_face.py` | 契约 payload ↔ 她的入口参数、她的结果 ↔ 契约响应 | 跟外部实现走 |
| 口径对照 | `agents_ext/levels.py` | 灾种键、风险等级、状态四态的双向对照 | 跟外部实现的词汇走 |

**为什么分三层**：翻译逻辑混进执行壳，会让"改一条映射"需要读懂整份订阅/幂等/超时代码，
而执行壳与翻译的**变化理由完全不同**（前者跟契约走，后者跟外部实现走）。
平台侧看不见她的函数签名，契约门禁（7 项一致性测试）与总线、网关、审计全都照旧。

三条不能含糊的口径（写进 `contract_face.py` / `levels.py` 的代码注释，并由用例钉住）：

1. **数据面不假装**：她的结论带 `source=agents_ext_mock`，读的人一眼看出数据出处；
2. **风险等级口径唯一**：她的 high/medium/low ↔ 平台的 1–5 双向映射，不引第三套口径；
3. **"没法判断"不能变成"没有风险"**：她的 status 四态（success / insufficient_data /
   uncertain / failed）逐个区分，**只有 success 才产出 risk_level**；数据不足走
   `E_EXT_INSUFFICIENT` 且不给定级。这一条是实现时才发现的：
   直译会把"数据不足"写成"无风险"——正是本仓反复禁止的"拿不到就说没有"。
   （错误码一律从 `levels.py` 的表里取：外部错误码要 ≤21 字符，
   早先那个 24 字符的 `E_EXTERNAL_AGENT_INSUFFICIENT_DATA` 会被网关当 schema 错误拒收，
   平台就看不到"数据不足"这个原因。）

### 3. 两套并存，不替换

`AEGIS_AGENT_IMPL=builtin|ext`，**默认 builtin 不变**。理由：自建内环接真实数据面、
已过契约 7 项一致性门禁；外部模块的数据工具目前是 Mock 源。替换是部署方的选择，不是本次并入的副作用。

**当前接通范围（2026-11-09 状态）**：外部实现**只接了研判段的 `assess.hazard` 一个动作**。
平台往感知段也发同名动作（`assess.hazard`，但要的是 `trigger_hits`），
决策段 `plan.stu`、执行段 `execute.warn`、反馈段 `feedback.attribute`
**明确不接线**——请求被类型化错误（`E_EXT_ACTION_UNSUP`）拒绝：走 error 通道、
事务记 error、**不计协同成功**。

**为什么"明确不接线"而不是"接了再说"**：接了之后回一个形状不对的载荷，平台侧会
"判不合契约再回落"——看着像降级，其实是接线失败，那会把一条没通的链路记成"外部实现跑过"。
宁可显式拒绝，让"没接"写在台账上。

### 4. 存档：原始物料与占位说明都进 `archive/`（不进主线构建）

- `archive/external_agents_source_20261109/`：外部仓库里不适合进主线的原始物料
  （18MB 灾害数据集 zip、三份 docx、运行产物、**抹除了密钥的**原 config.py）；
- `archive/agents_delivery_placeholders_20261109/`：我们自己的 `agents/` 五个占位 README 的
  并入前原貌——不删、不改写历史。

`archive/` 不在包路径上，不进 wheel/镜像，不被 mypy/ruff/pytest 扫描，只作溯源与复核。

### 5. 改动面尽量小，但该改的必须改

- **机械改写**（脚本化、可复现）：顶层包名 import（196 处 `from agents.`）→ 包内绝对路径；
  39 处 `sys.path.insert` 引导删除；空 `tools/` 占位不并入；
- **真实缺陷**（逐条核过语义等价）：与 `hazard_registry` 逐字相同的重复函数（违反该模块自己的
  单一真源原则）、三个死变量、一个未使用循环变量、except 里缺 `from exc`；
- **类型改造**：21 处节点返回值标注、6 处 `stdout.reconfigure` 安全调用、图构建返回类型、
  容器注解加宽；mypy 对本模块**精确到错误码**豁免 7 类（全部源于松散 dict 注解与
  langgraph 1.x 泛型收紧，不是真缺陷），真缺陷类错误照常被抓；
- **凭据治理**：`config.py` 只从环境变量读（平台口径 `AEGIS_EXT_LLM_API_KEY`）；运行时文件路径配置化。
  **一处如实记着的尾巴**：外部模块的 `llm.py` 还认原仓库那个 `ZHIPUAI_API_KEY` 环境变量名
  （两条路进同一个凭据）。它不是"把 Key 粘回源码"的路子，但 `.env.example` 只登记平台口径那一个。

**所有这些改动都固化在 `backend/scripts/` 的四个 Python 脚本里**
（`import_external_agents.py` + `import_external_agents_tests.py` + `postprocess_external_agents*.py`），
一条命令 `import_external_agents_all.sh` 可完整重跑。理由是踩过的坑：
手工改过的文件在重跑搬入时会被冲掉——密钥就是这样重新进过一次仓。

### 6. 边界用门禁钉住，不靠 review

`backend/tests/unit/test_agents_ext_boundaries.py`（AST 读源码）钉住三条最容易被改坏的边界：
① 外部模块不得直连数据库/存储层；② 不得依赖平台内部实现（否则不再是总线上的独立服务）；
③ 不得改全局 `sys.path`。另加"不写死绝对路径"与"不残留明文密钥"两条。
其中两条是**按文件参数化**的（`132 个文件 × 2 条` + 5 条独立判据 = 269 条），
所以条数会随 `agents_ext` 的文件数变动——它守的是"每个文件都过"，不是固定条数。

## 已知限制（如实记，不写"已解决"）

这节记的是**并入之后当下仍然成立的事实**。每条都对应代码里的一处具体行为，不是展望。

### 1. 数据面缺口：平台给的是"规则命中"，外部要的是"观测事件"

平台的研判请求带的是 `hits`（`rule_id` / `hazard_type` / `score` / `observed_at`），
外部因子引擎要的是**观测事件**（`processed_data.events`，含 `rainfall_mm_h` 这类灾种因子字段）。
适配层把 `hits` 转成事件骨架喂过去，但**因子字段缺失**，
外部因子引擎会按它自己的门控判 `insufficient_data`。

**结论：外部研判在当前形态下交不出结论，会诚实降级回平台规则轨**——
这是降级，不是接通。要让它真正产出结论，需要平台在研判请求里带上原始观测
（外部实现经平台数据 API 取数），本期不做。

还有一条**外部实现自己写死的声明**（在它 `risk_result` 的 `metadata.note` 里）：
"风险等级与组合指数基于 MOCK 因子权重与原型候选公式，非真实地质灾害风险预测，禁止用于实际预警决策"。
这句话比我们从外面判的限制还硬——它自己就说明了那份结论的用途边界。

### 2. 外部结果没有 `confidence` 字段

契约要求 `confidence`（平台会校验 0..1），外部的 risk_result 没有这一格。
适配层的处理是**不产结论**（`E_EXT_NO_CONFIDENCE`），而不是填 `0.0` 冒充——
填了会被平台当成一条有效研判采纳。

### 3. 线程里的图取消不掉

外部入口是同步函数（图在同进程内跑完），适配层放在 `asyncio.to_thread` 里跑
（直接在事件循环里调会阻塞总线上的其他消息）。代价是：`AgentKit` 的 deadline 只取消协程，
**超时后那张图仍在跑**（内部还有自己的重试环），会继续烧 CPU 与模型调用。
真要掐断得走进程隔离，不在本期范围。

### 4. 只有研判段接线

见 §3。感知段要的 `trigger_hits` 形状、决策/执行/反馈三段的动作翻译都还没写；
`contract_face.py` 里目前只有 `assess_face` 一条翻译函数。

### 5. 外部的 LLM 绕开平台网关

外部模块自建 `ChatOpenAI` 直连 OpenAI 兼容端点，**不经 `services/llm/llm_gateway.py`**——
即不经提示注入围栏（`prompt_guard`）、不进平台模型台账
（用量、模型、prompt 版本、结构化输出首过率都统计不到）。
结论的模型溯源字段 `ModelInfo` 只能填外部自己配置面给的模型名。

### 6. 共享记忆未打通

平台有 `services/memory/` 的共享记忆腿；外部模块的反馈记忆是它自己的一份 JSON 文件
（路径由 `AEGIS_EXT_FEEDBACK_MEMORY` 给）。两套记忆互不可见。

### 7. 131 处 `print()` 噪声

外部实现把过程输出直接打到 stdout（分布在 37 个文件里），不走平台的结构化 JSON 日志，
也不带 trace_id——这些行在平台日志里是"无主"的。

### 8. 外部的 manager 编排能力未接入

外部实现里有一个全局调度图（manager_agent：编排 data/risk/simulation/warning/feedback，
含重试与反馈返工闭环）。平台侧**只用了它的 risk_agent**——
manager 与其余四个 agent 的能力都在包里，但没接上。

## 后果

**好的**：
- 外部模块的**代码与能力**进了平台、走同一份契约：五灾种因子引擎、推演情景、预警文案、反馈闭环、
  manager 编排图都在包内；但**当前只接通了研判段**（见"已知限制"），其余是"在仓未接线"；
- 她的 109 条用例成了本仓的门禁资产（迁入 `backend/tests/unit/agents_ext/`，109 passed）；
- 外部实现自己也过了契约 7 项一致性门禁（`backend/tests/contract/test_agents_ext_conformance.py`，27 条），
  `tests/contract` 整体 87 条全绿；全量 pytest **3849 passed / 0 failed**（口径 unit + api + contract + e2e；
  `tests/integration` 与 `tests/perf` 另列，不进这个口径）；
- 两套实现在同一契约下可比可换，部署方按需要选。

**要付的代价**：
- 多一套依赖（langchain 系，约几十 MB）与一套 mypy/ruff 口径（精确豁免，不是全关）；
- 外部模块的数据面仍是 Mock，要接真实数据得另立一项（把它换成平台数据 API）；
- 外部仓库那把明文密钥**已经在她原仓库公开过**，必须在供应商侧轮换——本仓不持有它，
  但"我们曾经经手过它"这件事要求轮换而非仅仅删除；
- 外部实现的 LLM 绕开平台网关：不进模型台账、不经提示注入围栏（见"已知限制"第 5 条）。

**明确不做**：账号密码/OAuth（外部实现是部署方自管凭据的模块，不做平台级账号体系）；
把外部实现的数据工具换成平台数据 API（另立一项）；用它替换自建内环（默认仍 builtin）；
给它接平台共享记忆或做进程隔离来掐断图（另立一项）。
