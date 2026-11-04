# 智能助手是否需要开源 Agent 框架：选型调研

> 调研日期：2026-11-04｜代码基准：`main @ 03a2f14`
> 结论先行：**不建议引入开源 Agent 框架作为运行时**。项目缺的不是编排能力，而是"把平台动作安全暴露给自然语言"的权限化执行层；这一层没有现成框架直接给。
> 本文区分三类信息：**【官方核实】**（框架官方文档，2026-11-04 读取）、**【第三方】**（对比文章/评测，仅作参考）、**【项目实测】**（本仓库代码与文档）。

---

## 1. 结论

| 项 | 结论 |
| --- | --- |
| 是否引入框架作为底座 | **不引入**。现有自研 DAG + 五智能体 + 总线 + 记忆 + 观测 + 评测已覆盖框架的全部核心卖点 |
| 是否引入薄库 | **可选**。若后续要写更复杂的工具调用与条件审批，可小范围试点 Pydantic AI；不进入 P0–P2 关键路径 |
| 最值得借鉴的设计 | Pydantic AI 的 `requires_approval` / `ApprovalRequired`（按参数与角色条件审批）；LangGraph 的 `interrupt()` + checkpointer 语义 |
| 最不该选 | LangGraph / Microsoft Agent Framework 作为编排 runtime——与自研 DAG 形成第二套编排真源；OpenAI Agents SDK 默认上传 trace，内网合规不可接受 |
| 重新评估的触发条件 | 见 §7 |

---

## 2. 项目现有能力对账（框架卖点逐项核对）

| 框架常见卖点 | 本项目已有 | 证据 | 缺口 |
| --- | --- | --- | --- |
| Agent loop / 工具调用 | 12 个白名单动作、9 帧协议、规则优先 + LLM 只选动作 | `services/interpret/assistant.py:72-85, 402-431` | 动作目录要扩到全量业务能力 |
| 结构化输出 | `chat_structured`：JSON 模式 + Pydantic 校验 + 一次修复重试 | `services/llm/llm_gateway.py:7-10` | 无 |
| 多模型路由 | 按 task key 选模型与参数，未配则继承全局 | `llm_gateway.py:7` | 无 |
| 流式 | `chat_stream` + SSE 帧 | `llm_gateway.py:11` | 无 |
| 熔断/降级链 | provider 连续失败熔断、切备用、整链不可用抛错 | `llm_gateway.py:14-15` | 无 |
| Prompt 版本化 | `PromptRegistry` + 版本号写用量台账 | `llm_gateway.py:16` | 无 |
| 多智能体 handoff | NATS 总线 + 网关定向派发 + 五本体 + 事务台账 | `bus/gateway.py:159-205` | 无 |
| 图编排 / checkpoint | 自研 DAG 19 类节点、实例检查点落 PostgreSQL | `workflow/`、`pipeline/workflow_chain.py:109-153` | 无（且这是业务真源） |
| 长期记忆 | 四类记忆 + pgvector + Graphiti | `services/memory/`、`knowledge/` | 无 |
| 可观测 | OTel + Prometheus + Tracer 时延账本 + 用量台账 | `observability/`、`llm_gateway.py:12-13` | 无 |
| 评测 | `quality_eval`、`parsing_eval`、`model_cards` | `services/llm/` | 无 |
| HITL / 人工确认 | `/confirm` 五道校验 + `human_review` 工单 + 核签清单 | `assistant.py:364-395`、`review_queue.py:37-69` | 条件审批的声明方式可借鉴 |
| Guardrails | 越权拒绝、数字闸、提示词围栏、演练隔离 | `assistant.py:109-143`、`text_guard.py`、`prompt_guard.py` | 助手 LLM 尚未套围栏（已列入 FR-2.6） |
| MCP 工具面 | 7 项工具、独立令牌闸、调用时延 | `api/app.py:195-224`、REPORT §4.2 | 无 |

**判断**：14 项里 13 项有等价物，唯一实质增量是"类型化工具调用 + 条件审批"的**写法**，不是能力。用自研动作注册表 + Pydantic schema 即可覆盖，不需要为此引入一个运行时。

---

## 3. 候选框架事实（2026-11-04）

| 框架 | 形态 | 许可证 | 核心能力 | 与本项目的冲突点 |
| --- | --- | --- | --- | --- |
| **Pydantic AI** | Python 库（agent loop + 可选 Graph/Harness） | MIT【第三方一致】 | 类型化工具、结构化输出、`requires_approval` / `ApprovalRequired`、`DeferredToolRequests` 序列化恢复、durable execution（Temporal/DBOS/Prefect 等）、OTel 原生、模型无关 | 与自研 LLM 网关形成第二套模型调用路径；依赖增加 |
| **LangGraph** | 低层编排 runtime（state graph） | MIT【第三方一致】 | 显式状态图、checkpointer、`interrupt()` HITL、streaming、长期记忆 | 与自研 DAG 19 类节点、实例检查点直接重叠；容易变成第二套编排真源 |
| **Microsoft Agent Framework 1.0** | 库 + 图工作流（AutoGen + Semantic Kernel 合并） | MIT【官方 devblog】 | 1.0 GA 2026-04-03；图工作流、checkpoint/hydration、HITL、MCP/A2A、多 provider | 文档与示例 Azure 优先；发版快、有 breaking change；与 DAG 重叠 |
| **OpenAI Agents SDK** | 轻量库（Agents/Handoffs/Guardrails） | MIT【第三方一致；官方首页未列】 | Agent loop、handoff、guardrails、内置 tracing、tool approvals | **tracing 默认开启并上传 OpenAI 后端，`trace_include_sensitive_data` 默认 True**【官方】；内网应急系统不可接受 |
| **CrewAI** | 角色化多智能体库 | MIT core | 角色/任务/crew、快速原型 | 生产耐久性一般；多智能体对话会放大 token 成本【第三方】；与总线模型重复 |
| **Dify** | 低代码平台 | 修改版 Apache 2.0（限制多租户商业托管）【第三方】 | 可视化工作流、RAG、Agent | 项目已完成 Dify 迁移盘点并自研工作流，回头即倒退 |
| **n8n** | 自动化平台 | Sustainable Use License（非 OSI） | 连接器与流程自动化 | 不是 agent 库；许可证不适合作为底座 |

**核实说明**：版本号与发布日期来自官方博客/文档与第三方评测，只作方向参考；许可证以各仓库 `LICENSE` 为准，落地前需法务复核。Pydantic AI 官方首页未标版本号，本文只描述其能力形态，不写具体版本。

---

## 4. 关键事实：框架不给"权限化执行"

Pydantic AI 官方文档在 deferred tools 一节明确写道：HITL 审批**不能替代**端点鉴权，也**不能替代**工具函数内部对敏感操作的授权校验；UI 适配器甚至无法在服务端记录自己签发过哪些调用，能访问端点的客户端可以为自己捏造的调用批准。

这正好说明本项目的核心工作不在框架里：

| 要做的事 | 框架是否提供 | 本项目怎么做 |
| --- | --- | --- |
| 动作目录（全量业务能力 → 可调用动作） | 不提供，需自己定义 tools | `ACTION_SPECS` 扩展为注册表 |
| 角色能力矩阵（谁能做什么） | 不提供 | `operators` + 服务端校验 |
| 影响预览（对象/受众/通道/不可逆项） | 不提供 | 统一执行器生成 preview |
| 审计到人（谁、说了什么、做了什么） | 只给 trace，不给业务审计 | `control_actions` + `audit_ops` |
| 撤销与幂等 | 不提供 | 撤销窗口 + `idempotency_key` |
| 演练/真实隔离 | 不提供 | `drill` 标识贯穿事件/卡片/审计 |

**因此：引入框架不能减少这部分工作量，只会增加一层适配。**

---

## 5. 决策矩阵（权重按本项目约束设定）

打分 1–5，5 为最契合；权重反映本项目"稳定交付 + 安全合规 + 不与现有真源冲突"的优先级。

| 维度 | 权重 | 自研扩展 | Pydantic AI | LangGraph | MS Agent Framework | OpenAI Agents SDK | CrewAI |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 与现有 DAG/总线不冲突 | 25% | 5 | 4 | 2 | 1 | 3 | 2 |
| 权限/HITL 粒度 | 20% | 5 | 5 | 4 | 4 | 4 | 2 |
| 类型安全/结构化输出 | 15% | 4 | 5 | 4 | 4 | 3 | 2 |
| 模型无关/内网离线 | 15% | 5 | 5 | 5 | 4 | 2 | 4 |
| 可观测/审计 | 10% | 5 | 4 | 4 | 4 | 3 | 3 |
| 依赖/供应链/许可 | 10% | 5 | 4 | 3 | 2 | 3 | 3 |
| 迁移/学习成本 | 5% | 5 | 4 | 2 | 2 | 4 | 4 |
| **加权总分** | 100% | **4.85** | **4.50** | 3.45 | 2.95 | 3.10 | 2.60 |

> 打分是相对排序，不是绝对结论；权重换一组，Pydantic AI 可能接近自研。真正稳定的结论是：**LangGraph / MS AF 与自研 DAG 冲突最大，OpenAI SDK 的默认遥测最不适合内网。**

---

## 6. 推荐方案

### 方案 A（推荐）：自研扩展，零新依赖

- 把 `ACTION_SPECS` 扩为全量业务动作注册表，字段：`action_id`、`domain`、`required_role`、`params_schema`、`target_resolver`、`reversible`、`needs_confirm`。
- 统一执行器：解析 → 目标解析 → 参数校验 → 权限校验 → 影响预览 → 执行 → 回执 → 审计。
- 复用现有 `chat_structured` 做意图与参数抽取；复用 `PromptRegistry` 管提示词版本；复用 Tracer 记时延。
- 借鉴而非依赖：照 Pydantic AI 的 `ApprovalRequired` 语义，把"按参数/角色条件审批"写进 `needs_confirm` 判定；照 LangGraph 的 `interrupt` 语义，把多步指令的暂停/恢复挂到现有工作流实例检查点。

**代价**：动作注册表要自己维护；**收益**：零新依赖、零第二套真源、审计与权限完全可控。

### 方案 B（可选试点，P1 之后）：Pydantic AI 只做"助手内环"

- 仅在"意图 → 工具选择 → 条件审批 → 结果"这一段用 Pydantic AI，业务执行仍调既有服务。
- 用 feature flag 隔离，不替换 `llm_gateway` 主路径；先跑一个月的对照（时延、首过率、误调用率）。
- 触发条件：动作数超过 40 个、或条件审批规则复杂到自研判定难以维护。

### 方案 C（不推荐）：LangGraph / MS AF 作为编排 runtime

- 会把"业务编排"从自研 DAG 迁到框架图，形成第二套真源；现有五段链路、实例检查点、降级台账都要重做适配。
- 唯一合理场景：未来要做"开放式多智能体自主协作"或"跨天长任务"，且愿意把 DAG 整体迁移——那是平台级重构，不是助手升级。

---

## 7. 重新评估的触发条件

出现以下任一情况，再启动框架选型：

1. 助手从"白名单指令"升级为**开放式自主协作**（多智能体自行规划、互相 handoff，不再逐步指令）。
2. 出现**跨小时/跨天**的长任务，且现有工作流实例检查点不够用（需要 replay、时间旅行、跨部署恢复）。
3. 要让**非工程师**用可视化方式配置助手行为（那时应考虑平台型工具，而不是代码库）。
4. 助手独立成产品、需要多租户与按租户隔离。
5. 动作注册表超过约 40 个动作或 10 个服务，自研维护成本明显超过框架适配成本。

---

## 8. 参考来源（2026-11-04 读取）

| 来源 | 用途 |
| --- | --- |
| https://ai.pydantic.dev/ | Pydantic AI 能力、模型无关、durable execution、OTel |
| https://ai.pydantic.dev/deferred-tools/ | `requires_approval`、`ApprovalRequired`、`DeferredToolRequests`、条件审批与持久化恢复；HITL 不等于端点鉴权的官方警告 |
| https://docs.langchain.com/oss/python/langgraph/overview | LangGraph 定位、checkpointer、interrupt、可独立使用 |
| https://openai.github.io/openai-agents-python/ | Agents/Handoffs/Guardrails、默认 OpenAI Responses、非 OpenAI 适配 |
| https://openai.github.io/openai-agents-python/tracing/ | tracing 默认开启并上传 OpenAI、`trace_include_sensitive_data` 默认 True、禁用与替换方式 |
| https://devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0/ | Microsoft Agent Framework 1.0 GA、AutoGen + Semantic Kernel 合并、图工作流与 HITL |
| 第三方对比文章（aitoolsatlas / paperclipped / agentnative 等） | 版本、stars、学习曲线、成本等方向性信息，未作为决策依据 |

**项目侧证据**：`backend/pyproject.toml` 仅依赖 `openai>=1.50`，无任何 agent 框架；`services/llm/llm_gateway.py` 已实现六项能力；`docs/audit/DIFY_MIGRATION_INVENTORY.md` 记录了从 Dify 迁出的过程与理由。
