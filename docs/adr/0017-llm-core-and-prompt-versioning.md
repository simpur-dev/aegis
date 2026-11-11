# ADR-0017：LLM 通路共享内核与提示词版本化

- 状态：已采纳（2026-11-11，分两阶段落地）
- 关联：ADR-0013（灾种侧 LLM 熔断与客户端复用）、ADR-0016（统一智能体层与能力矩阵）、ADR-0007（D10 溯源）

## 背景

ADR-0016 把智能体层统一为 11 个本体 + 一张能力矩阵，但**还有最后一块"两套"没拆**：
LLM 调用通路与提示词版本化。两侧的差距不是"风格不同"，是**同一个"上游挂了"给出两种行为**：

| 维度 | 平台网关 | 灾种模块 |
|---|---|---|
| 熔断 | 按模型名分账，一开闸就**永久**跳过 | 带 60s 冷却 + 半开试探 |
| 失败分类 | **一切** provider 异常都计入熔断 | 只认传输层失败（连接/超时/5xx/429） |
| SDK 盲重试 | `AsyncOpenAI` 默认 `max_retries=2`（**开着**） | `max_retries=0`（已关） |
| 记账 | `LlmCallRecord` 台账 + tracer 埋点 + 首过率 + 预算告警 | **零记账、零埋点** |
| 提示词 | 注册表版本化（6 条） | 模块级 Python 常量（15 条 system + 11 个 builder），**无版本** |
| D10 凭证 | `Reply.model_info` 有值 | 8 处 `Reply()` 全不带 `model_info` |

灾种侧的熔断文件（`agents/hazards/llm_breaker.py`）自己写着移除条件："一旦外部实现的 LLM
改走平台网关……本文件即可整体删除"。本 ADR 不走那条路——理由见下。

## 决策

### 阶段 1：先修平台的三个既有缺陷（独立提交，行为变更）

三项都是平台**当前**的缺陷，与结构重构分开落地，便于单独回归与单独回滚。

1. **熔断加冷却窗口**（`Settings.llm_breaker_cooldown_seconds`，默认 60s）。
   此前 `_open[model]=True` 之后该模型永不入候选，`_note_success` 因此不可达——
   **上游恢复了也永远用不上**，只能靠人改配置。
2. **4xx 不计入熔断**。4xx（429 除外）是请求本身错了（参数、鉴权、路径），重试没有意义，
   却会让模型被下架——把"调用方写错了"变成"服务不可用"是最贵的误判。
   台账仍记 `validation="transport_error"`（事实），只是不计入熔断（策略）。
3. **关掉 SDK 盲重试**（`AsyncOpenAI(..., max_retries=0)`）。盲重试把一次失败变成三次
   HTTP 往返；灾种侧实测它占研判时延的 **81%**（ADR-0013）。

> **1 与 3 必须同批上线**：盲重试关了而熔断还是永久开，上游恢复后平台就永远不再重试
> 那个模型——比现状更糟。两者在同一提交内落地。

**为什么熔断的实现放在 `aegis/agents/core/`（L3）而不是 `services/llm/`（L4）**：
灾种包的边界门禁是白名单制（`test_hazards_boundaries.py`），内核若留在 L4，灾种包
import 它就是 L3→L4 倒挂。平台自己的 `agents/assess.py:29` 已经这么干过一次
（import `services.llm.prompt_guard`），口径要统一。`agents/core/` 是智能体层自己的
契约面，与 `agentkit.py`/`capabilities.py` 同级。

### 阶段 2：共享内核 + 两个 provider 适配器

```
agents/core/           # 共享内核：横切语义，两侧共用一份实现
├── breaker.py         # 熔断状态机 + 失败分类口径
├── prompts.py         # 提示词注册表（唯一真源，版本化）
├── ledger.py          # LLM 调用台账 + 时延埋点
├── meta.py            # 调用元数据与 trace_id 的跨线程上下文
└── providers.py       # provider 错误分类策略

services/llm/          # 平台 provider 适配器（AsyncOpenAI / json_object + 修复重试）
agents/hazards/llm.py  # 灾种 provider 适配器（Zhipu ChatOpenAI / function_calling 双通道）
```

**共享的判据**："两个 provider 都要用、且用起来不该有两套行为"的归内核；
**"只有某家厂商才有"的留在各自的适配器里**——具体就是 Zhipu 的 function_calling
双通道（`hazards/llm.py:129-172`），那是**厂商 quirk**，搬进通用网关等于让网关背上
某家厂商的分支。

**为什么不走"灾种改调平台网关"那条路**：那要给网关加多 provider 路由（单端点单凭据
→ 按 task 选端点与凭据），并把 Zhipu 的结构化输出兼容层搬进通用网关——那是**新功能**，
不是"合并两套实现"，且会让平台网关从通用件退化成认识某厂商 quirks 的件。

### 提示词版本化

唯一注册表共 22 条：平台 6 条（现状不动）+ 灾种 16 条（11 基础 + 5 重试变体）。
灾种键名 `hazard.<节点>`，重试变体 `hazard.<节点>.retry`，版本一律 `1.0.0`
（= 当前文本的逐字版本）。

**重试变体为什么必须独立成条**：灾种侧 5 个 prompt builder 带条件追加
（`if retry_count > 0: base += ...`）。`str.format` 表达不了条件——可以用变量表达，
但那样"重试段文本"就落在注册表之外，D10 的版本号就不描述实际发送的正文（谎话）。
拆成两条模板后，两段文本各自有版本号，`prompt_version` 如实反映本次用的是哪条。

**正文里的花括号**：已核实灾种 15 条 system 文本与 11 个 builder 的模板骨架里**没有字面
`{` `}`**（`feedback_prompt.py` 完全无匹配；其余全是 f-string 插值）。builder 的**运行时
输出**必然内嵌上游 JSON（`assessment_json` 等），那些是**变量值**，`str.format` 不再处理
替换值的花括号，安全。

### D10 凭证与命名冲突

灾种本体 8 处 `Reply()` 回填 `model_info=ModelInfo(model=<provider 模型名>,
prompt_version=<主 prompt 版本>)`。每个本体一个**主 prompt**（结论由它定）。

**命名冲突**：灾种 payload 内已有一个 `model_info` **业务 dict**
（`risk_output_node.py:115-120` 的 `rule_version`/`engine_version`/`is_mock`），
与平台 `ModelInfo`（信封字段）是两回事——**同名不同层**。信封的 `model_info` 是 D10
凭证；payload 内的是业务字段（因子引擎的版本与 mock 标记），因子引擎版本**不在** D10 里
重复。

### trace_id 通路

灾种节点此前拿不到 trace_id（`manager_agent/state.py:106` 的 trace_id 只用于发总线
请求；各腿 state 无该字段），而平台侧 `aegis/logging.py:12` 的 `trace_id_var` 全仓
**没有任何 `.set()` 调用**——JSON 日志里从来就没有 trace_id。用一个 ContextVar 打通
两侧：灾种本体在 `handle()` 里 set，`pool.run_blocking` 的 `copy_context()` 把它穿透进
节点；平台侧在网关入口同样 set。

## 后果

- 正面：熔断/记账/版本/错误分类**各一份实现**；平台继承带冷却的熔断与关掉的盲重试；
  灾种继承记账、时延埋点、prompt 版本与 D10 凭证。
- 代价：平台**行为变更**三处（熔断会自愈、4xx 不再下架模型、失败路径从三次往返变一次），
  单独一个提交，便于单独回滚；灾种 prompt 从"模块级常量"变成"注册表条目"，
  `agents/hazards/prompts/` 目录消失。
- 不在本次范围：灾种改走平台网关；`AEGIS_EXT_*` → `AEGIS_HAZARD_*` 的改名与旧名兼容
  读取（同批落地，见提交说明）。

## 落地记录（2026-11-11）

| 阶段 | 提交 | 内容 |
|---|---|---|
| 阶段 1 | `51db9dc` | 平台三项缺陷修复 + 共享熔断内核（`agents/core/breaker.py`） |
| 阶段 2a | `decbefc` | 提示词全量迁入唯一注册表（21 条），删 `hazards/prompts/` |
| 阶段 2b | `bac0fcc` | 灾种 provider 改用共享熔断与共享台账，删第二份 `llm_breaker.py` |
| 阶段 2c | `c31caf2` | D10 凭证贯通（8 处 `Reply`）+ trace_id 跨线程通路 |
| 阶段 2d | `8d65f40` | 环境变量族改名 `AEGIS_EXT_*` → `AEGIS_HAZARD_*`（旧名兼容） |

### 实测证据

- **提示词迁移逐字保真**：正文用 AST 从原 f-string 抽取（不手抄），再逐条渲染比对——
  **15/15 与原 builder 输出逐字一致**（含换行、全角标点、空格）。其中一个重试变体
  （`hazard.scenario_design.retry`）因对应节点**从不传 `retry_count`**（原分支永不可达）
  而没有登记——登记一条用不到的就是死代码。
- **全量门禁**：4277 通过 / 0 失败 / 119 跳过；ruff 与 mypy 全绿。
- **新增门禁**：`test_llm_breaker_core.py`（19 条，熔断状态机与失败分类）、
  `test_hazard_prompt_versions.py`（3 条，版本号 + 正文指纹防"改正文忘升版本"）。

### 一处记录更正

`1cf35b9` 的提交信息写"删了 `manager_prompt.py` 的 8 个死符号"，实际只删掉了 4 个
函数——那次脚本在常量替换处断言失败、文件没落盘。4 个常量（`MANAGER_TASK_ANALYZE_PROMPT` /
`MANAGER_TASK_PLAN_PROMPT` / `MANAGER_ERROR_ANALYSIS_PROMPT` / `MANAGER_FEEDBACK_REVIEW_PROMPT`）
至今还在，已在 `51db9dc` 补删。这条更正留在 ADR 里，因为"提交信息说了不等于做成了"。

## 后续项（不在本次范围）

1. **灾种 LLM 通路改走平台网关**：需要给网关加多 provider 路由（单端点单凭据 → 按 task
   选端点与凭据）并把 Zhipu 的 function_calling 兼容层搬进通用网关——那是**新功能**，
   不是"合并两套实现"，且会让平台网关从通用件退化成认识某厂商 quirks 的件。
2. **`AEGIS_EXT_*` 旧名的移除**：现网部署迁移完成后才能删兼容读取分支（保留一个发布周期
   是稳妥的）。
3. **灾种 prompt 的语义演化**：15 条模板现在有了版本号，后续任何正文改动都要升版本并
   同步 `test_hazard_prompt_versions.py` 的指纹——那正是 D10 要的可追溯性。
