# agents/ — L3 五大智能体交付落点

> 定位：感知 / 研判 / 决策 / 执行 / 反馈五类智能体由**智能体方（外部团队）实现**，
> 本目录是它们的交付落点与联调脚手架。平台侧不实现智能体本体——
> `backend/src/aegis/agents/mock.py` 是参考实现与一致性门禁的对照源，不是产品。
>
> 唯一接口契约：[`contracts/AGENT_INTEGRATION_SPEC.v1.md`](../contracts/AGENT_INTEGRATION_SPEC.v1.md)
> 消息 Schema：`contracts/agent_message.v1.schema.json`、`contracts/stu.v1.schema.json`

## 目录

| 目录 | 智能体 | 契约动作（出向） | 状态 |
| --- | --- | --- | --- |
| [`perceive/`](perceive/) | 感知 | `perceive.anomaly`、`perceive.trigger_hit` | 未交付 |
| [`assess/`](assess/) | 研判 | `assess.risk_level` | 未交付 |
| [`plan/`](plan/) | 决策 | `plan.stu_result` | 未交付 |
| [`execute/`](execute/) | 执行 | `execute.ack` | 未交付 |
| [`feedback/`](feedback/) | 反馈 | `feedback.status` | 未交付 |

状态流转：`未交付 → 开发中（本地联调）→ 门禁中 → 已接入（生产总线）`。
接入证据 = `backend/tests/contract/` 7 项全绿 + 单灾种场景端到端跑通（见下"接入流程"第 5 步）。

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
