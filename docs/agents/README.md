# agents/ — L3 智能体层的接入指引与能力索引

> 定位：本目录是智能体层的**接入指引与能力索引**。实现代码不在这里
> （这个目录不在包路径上，代码不进 wheel/镜像）。
> **注意口径已经变了两次**（2026-11-09 并入 → 2026-11-11 统一）：
> 平台自建五段内环（`backend/src/aegis/agents/` 顶层）与灾种闭环六体
> （`backend/src/aegis/agents/hazards/`）现在是**一套统一的智能体层**——
> 11 个本体同挂一条总线，能力与归属的单一真源是
> `backend/src/aegis/agents/capabilities.py`（每项能力恰好一个主人）。
> **不存在实现选择开关**（`AEGIS_AGENT_IMPL` 已删）。
> `backend/src/aegis/agents/mock.py` 是契约门禁的对照源，不是产品。
>
> 唯一接口契约：[`contracts/AGENT_INTEGRATION_SPEC.v1.md`](../../contracts/AGENT_INTEGRATION_SPEC.v1.md)
> 消息 Schema：`contracts/agent_message.v1.schema.json`、`contracts/stu.v1.schema.json`
> 外部交付未落地期间的**自建方案**（LLM 化改造、框架选型、验收重测，含平台侧配套）：
> [`docs/课题6_架构升级方案.md`](../课题6_架构升级方案.md)（决策记录：ADR-0007）

## 已并入的灾种闭环模块（2026-11-09 并入；2026-11-11 统一进 agents 包）

一份外部交付的智能体模块（LangGraph 多智能体实现）已并入主线，落在
**`backend/src/aegis/agents/hazards/`**，与平台自建五段**同属一套智能体层**。

**它没有适配层**：本体直接继承 `AgentKit`、直接吃平台 DTO——
（并入当时曾有 `adapter.py` / `contract_face.py` / `levels.py` 三层，同日收口时全部删除；
现在若这三个模块存在反而是缺陷，`tests/contract/test_hazards_conformance.py` 用
`ModuleNotFoundError` 钉住了这一点。决策见 [`docs/adr/0012`](../adr/0012-assess-contract-unification.md)。）

六个本体各占一项能力（能力矩阵 `backend/src/aegis/agents/capabilities.py`）：

| 本体（agent_id） | capability | 动作 | 派发点 |
| --- | --- | --- | --- |
| `assess.data01` | `data_quality` | `assess.data_quality` | 生产研判段（证据门） |
| `assess.factor01` | `factor_risk_assess` | `assess.factor_risk` | 生产研判段（并行交叉验证） |
| `assess.sim01` | `scenario_sim` | `assess.scenario_sim` | 灾种闭环内腿 |
| `execute.hazard01` | `warn_content` | `execute.warn_content` | 生产执行段（并行素材腿） |
| `feedback.audit01` | `chain_audit` | `feedback.chain_audit` | 灾种闭环内腿 |
| `plan.loop01` | `hazard_workflow` | `plan.hazard_workflow` | `POST /api/v1/hazards/workflow` |

三条要说清的事实：① 它的数据工具目前是**内存 Mock 源**，结论带
`source=hazard.factor_engine` 与 `data_provenance.is_mock=true`，不冒充平台实测数据；
② 它的凭据只从环境变量读（`AEGIS_EXT_LLM_API_KEY`），源码里没有密钥；
③ **它能交结论**——平台把窗口观测 + 区域画像组成 `AssessEvidence` 随请求推送
（出进程部署也可经 `GET /api/v1/assess/evidence` 拉取），因子引擎按确定性策略自产
`confidence`（`0.15 + 0.40×coverage + 0.05×validation`，封顶 0.60）并返回定级结论。
真栈实测：`540121` 因子腿出带溯源的一等结论；覆盖率不足时如实拒绝——
**不会把"算不出"写成"无风险"**。
（并入当时"交不出结论"的缺口已由 [`docs/adr/0012`](../adr/0012-assess-contract-unification.md) 关闭。）

其余已知限制（线程里的图取消不掉、外部的 LLM 绕开平台网关、共享记忆未打通等）
详见 [`docs/architecture.md` §3.1](../architecture.md) 的"已知限制"一节。
`docs/adr/0011` 记的是**并入当时**的决策原貌，其中三层适配层与"交不出结论"两条已被 ADR-0012 取代，
文件头有横幅，不要按它实现接线。

LLM 通路与平台**共用一个内核**（[`docs/adr/0017`](../adr/0017-llm-core-and-prompt-versioning.md)）：
熔断（带冷却窗口）、提示词注册表、调用台账、调用元数据都在 `agents/core/`，两侧共用一份实现；
只有"厂商特有"的部分（Zhipu 的 `function_calling` 双通道）留在灾种 provider 里。

- **提示词版本化**：15 条灾种模板与平台 6 条同处一个注册表（键名 `hazard.<节点>`，
  重试变体 `hazard.<节点>.retry`），正文逐字迁移、版本号 `1.0.0`；
- **D10 凭证**：六个本体的回执都带 `model_info`（模型 + prompt 版本），
  `prompt_version` 能在注册表里查到；
- **记账**：调用落共享台账，用量/首过率/时延按 agent×model 归因——此前是零。
  口径已与平台对齐：token 取**真实用量**（响应 usage，缺省退到消息 `usage_metadata`）；
  双通道回退的通道二记 `attempt=1`（修复轮不进首过率分母，否则首过率系统性虚高）；
  "HTTP 200 但没产出合法对象"改判 `malformed`（算"需修复"，不让自然语言冒充首过）。
  两本台账（网关随实例那本 + 灾种进程级那本）在 `container.latency_report()` 的治理
  出口里并成一本算——灾种的用量/成本/首过率此前在报表里整体缺席。
- **环境变量**：`AEGIS_HAZARD_*`（旧名 `AEGIS_EXT_*` 仍可读；**四处兜底都记** deprecation
  日志，每变量一次——"还有谁在用旧名"看得见，才谈得上删兼容分支）。

熔断实测：上游不可用时单次研判 **2952 ms → 4.14 ms**（同口径实测，见 [`docs/REPORT.md`](../REPORT.md)）。

## 目录

| 目录 | 智能体 | 契约动作（出向） | 状态 |
| --- | --- | --- | --- |
| [`perceive/`](perceive/) | 感知 | `perceive.anomaly`、`perceive.trigger_hit` | 未交付；感知段由平台自建内环承接 |
| [`assess/`](assess/) | 研判 | `assess.risk_level` | 未交付；平台自建内环 + 灾种因子引擎（`assess.factor_risk`）均已在册 |
| [`plan/`](plan/) | 决策 | `plan.stu_result` | 未交付；平台自建内环 + 灾种闭环编排（`plan.hazard_workflow`）均已在册 |
| [`execute/`](execute/) | 执行 | `execute.ack` | 未交付；平台自建内环 + 灾种素材腿（`execute.warn_content`）均已在册 |
| [`feedback/`](feedback/) | 反馈 | `feedback.status` | 未交付；平台自建内环 + 灾种审计腿（`feedback.chain_audit`）均已在册 |

状态流转：`未交付 → 开发中（本地联调）→ 门禁中 → 已接入（生产总线）`。
接入证据 = `backend/tests/contract/` 7 项全绿 + 单灾种场景端到端跑通（见下"接入流程"第 5 步）。
灾种模块那一侧另有两组门禁：契约一致性 `backend/tests/contract/test_hazards_conformance.py`、
边界守卫 `backend/tests/unit/test_hazards_boundaries.py`（数据边界**唯一真源**，白名单制；
按文件参数化，条数随文件数变动）。

## 边界三原则（违反任一条过不了门禁）

1. 智能体是**总线上的独立服务**：不 import 平台代码、不直连平台数据库、不依赖平台内部实现细节；
2. 唯一交互载体是 **AgentMessage v1**：跨边界交互必须落成合法消息，由平台总线网关 100% 校验；
3. 数据经**引用而非内嵌**：载荷 ≤64KB，大数据放 `refs`，经平台数据 API（只读、服务令牌）取回。

## 接入流程

1. **读契约**：通读接入规范（subject 命名、动作注册表、错误码、心跳生命周期、量测点）与两份 JSON Schema。
2. **起平台**：本机联调只需 NATS + 后端——
   ```bash
   docker run -d --name aegis-nats -p 4222:4222 nats:2.10-alpine -js -sd /data
   cd backend && uv run python -m aegis.main      # API: http://127.0.0.1:8000/docs
   ```
   注意：智能体缺位时平台全链路照常运行（规则引擎降级），`GET /api/v1/agents` 可看到当前注册表；你的智能体注册成功后会出现在这里。
3. **对照参考实现开发**：`backend/src/aegis/agents/mock.py` 演示了五类智能体的完整契约行为
   （注册、心跳、各动作的请求-应答、错误回执），可作行为样板；语言不限，代码放对应子目录。
4. **过一致性门禁**：`backend/tests/contract/test_agent_conformance.py`（平台提供，Mock 总线驱动）。
   7 项：Schema 合法 / 幂等 / 超时语义 / 错误回执 / 心跳与注册 / 数据边界（禁数据库直连）/ 上下文快照恢复。
5. **单灾种端到端**：`uv run python -m scripts.drill --scenario surge`，链路各段执行方式（agent/local）
   在 `/dashboard` 与 `GET /api/v1/events` 可见——你的智能体接管的段应显示 agent。
6. **部署接线**（接入时才做，现在 compose 里没有占位服务）：在 `deploy/docker-compose.yml` 追加
   `agents` profile 的服务项，模板：
   ```yaml
   perceive-agent:
     image: <智能体方镜像>
     profiles: ["agents"]
     environment:
       AEGIS_NATS_URL: nats://nats:4222
       AEGIS_AGENT_ID: perceive.external01
     depends_on: [nats]
   ```
   整栈一律用同一份 compose 起（依赖容器挂默认 bridge 而 compose 建独立网络时，服务名会解析不到，
   且各条腿会各自优雅降级把"没接上"伪装成"部署成功"——这个坑的取证记录见 `docs/REPORT.md`）。

## 指标量测与归因（智能体方需要知道的）

协同成功率、共享同步时延、调度/重调度时延的**量测点全部在平台侧网关**（规范 §8），
与智能体实现质量解耦，按 `agent_id` 归因输出——性能数据平台会反馈，不需要自证。

## 目录规约

- 智能体代码放各自子目录（`perceive/` 等），构建产物、权重、虚拟环境不进 git；
- 每个子目录的 README 描述该智能体的职责与契约要点，交付时在其中追加实现说明与运行方式；
- 契约变更走语义化版本 + 一致性测试同步更新 + ADR 记录（规范 §9），不要单方面改 subject 或动作语义。
