# agents/ — L3 五大智能体交付落点

> 定位：感知 / 研判 / 决策 / 执行 / 反馈五类智能体由**智能体方（外部团队）实现**，
> 本目录是它们的交付落点与联调脚手架。
> **注意口径已经变了**（2026-11-09 起）：平台侧不再"不实现智能体本体"——
> 现在是**两套并存**：平台自建内环（`backend/src/aegis/agents/`，接真实数据面）与
> 已并入主线的外部交付模块（`backend/src/aegis/agents_ext/`），两者走同一份契约，
> 由 `AEGIS_AGENT_IMPL` 选择（默认 builtin）。本目录仍是**交付落点索引**，
> 实现代码不在这里（这个目录不在包路径上，代码不进 wheel/镜像）。
> `backend/src/aegis/agents/mock.py` 是契约门禁的对照源，不是产品。
>
> 唯一接口契约：[`contracts/AGENT_INTEGRATION_SPEC.v1.md`](../contracts/AGENT_INTEGRATION_SPEC.v1.md)
> 消息 Schema：`contracts/agent_message.v1.schema.json`、`contracts/stu.v1.schema.json`
> 外部交付未落地期间的**自建方案**（LLM 化改造、框架选型、验收重测，含平台侧配套）：
> [`docs/课题6_架构升级方案.md`](../docs/课题6_架构升级方案.md)（决策记录：ADR-0007）

## 已并入的外部实现（2026-11-09）

一份外部交付的智能体模块（LangGraph 多智能体实现）已经并入主线，落在
**`backend/src/aegis/agents_ext/`**，经**三层适配**走**同一份 `AgentMessage v1` 契约**，
与平台自建内环**并存**（`AEGIS_AGENT_IMPL=builtin|ext`，默认 `builtin` 不变）：

| 层 | 文件 | 职责 |
| --- | --- | --- |
| 契约壳 | `agents_ext/adapter.py` | `AgentKit.handle` 样板（订阅/心跳/幂等/deadline/错误回执）+ 动作分派 |
| 语义翻译 | `agents_ext/contract_face.py` | 契约 payload ↔ 外部入口参数、外部结果 ↔ 契约响应 |
| 口径对照 | `agents_ext/levels.py` | 灾种键、风险等级、状态四态的双向对照 |

实现与它的六类 agent 一一对应：

| 本目录 | 外部实现里的对应物 | 契约动作（出向） |
| --- | --- | --- |
| `perceive/` | `agents_ext/data_agent/`（数据获取与质量校验） | `perceive.anomaly`、`perceive.trigger_hit` |
| `assess/` | `agents_ext/risk_agent/`（五灾种因子引擎） | `assess.risk_level` |
| `plan/` | `agents_ext/manager_agent/`（全局调度图，平台侧取其任务拆解） | `plan.stu_result` |
| `execute/` | `agents_ext/warning_agent/`（预警文案与 CAP 结构） | `execute.ack` |
| `feedback/` | `agents_ext/feedback_agent/`（反馈闭环与建议） | `feedback.status` |

**当前接通范围（2026-11-09）**：外部实现**只接通了研判段的 `assess.hazard` 一个动作**。
感知段（平台也发 `assess.hazard`，但要的是 `trigger_hits`）、决策段 `plan.stu`、
执行段 `execute.warn`、反馈段 `feedback.attribute` **明确不接线**——
请求被类型化错误 `E_EXT_ACTION_UNSUP` 拒绝：走 error 通道、事务记 error、**不计协同成功**。
"在仓里"不等于"接通了"，下表按这个口径写。

三条要说清的事实：① 它的数据工具目前是**内存 Mock 源**，结论带 `source=agents_ext_mock` 标记，
不冒充平台实测数据；② 它的凭据只从环境变量读（`AEGIS_EXT_LLM_API_KEY`），
源码里没有密钥；③ **外部研判当前形态下交不出结论**——平台研判请求给的是规则命中（`hits`），
外部因子引擎要的是观测事件（含 `rainfall_mm_h` 等灾种因子字段），字段缺失会让它判数据不足，
于是诚实降级回平台规则轨；外部的 risk_result 也**没有 `confidence` 字段**（契约要求），
适配层不产结论而不是填 `0.0` 冒充。

其余已知限制（线程里的图取消不掉、外部的 LLM 绕开平台网关、共享记忆未打通等）
详见 [`docs/adr/0011`](../docs/adr/0011-external-agent-module-merge.md) 的"已知限制"一节
与 [`docs/architecture.md` §3.1](../docs/architecture.md)。

它的 LLM 调用自带熔断（[`docs/adr/0013`](../docs/adr/0013-ext-llm-breaker-and-client-reuse.md)）：
连续失败达阈值后在冷却窗口内直接跳过 LLM，研判走确定性兜底——上游不可用时
单次研判 **2952 ms → 4.14 ms**（同口径实测，见 [`docs/REPORT.md`](../docs/REPORT.md)）。

## 目录

| 目录 | 智能体 | 契约动作（出向） | 状态 |
| --- | --- | --- | --- |
| [`perceive/`](perceive/) | 感知 | `perceive.anomaly`、`perceive.trigger_hit` | 未交付；外部实现已并入，**感知段未接线**（明确不接线） |
| [`assess/`](assess/) | 研判 | `assess.risk_level` | 未交付；外部实现已并入，**研判段已接线**（`assess.hazard`，当前唯一接线的动作） |
| [`plan/`](plan/) | 决策 | `plan.stu_result` | 未交付；外部实现已并入，**决策段未接线**（明确不接线） |
| [`execute/`](execute/) | 执行 | `execute.ack` | 未交付；外部实现已并入，**执行段未接线**（明确不接线） |
| [`feedback/`](feedback/) | 反馈 | `feedback.status` | 未交付；外部实现已并入，**反馈段未接线**（明确不接线） |

状态流转：`未交付 → 开发中（本地联调）→ 门禁中 → 已接入（生产总线）`。
接入证据 = `backend/tests/contract/` 7 项全绿 + 单灾种场景端到端跑通（见下"接入流程"第 5 步）。
外部实现那一侧另有两组门禁：契约一致性 `backend/tests/contract/test_agents_ext_conformance.py`（27 条）、
边界守卫 `backend/tests/unit/test_agents_ext_boundaries.py`（按文件参数化，条数随文件数变动）。

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
