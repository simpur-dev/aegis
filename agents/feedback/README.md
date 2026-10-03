# 反馈智能体（feedback）

预警发布后的触达/响应/效果反馈：汇总回执与现场反馈，形成"预警—响应—反馈"闭环，反哺阈值与预案。

## 契约要点

| 项 | 值 |
| --- | --- |
| Subject（入） | 订阅 `platform.alert.>`（已确认告警广播）与 `agent.execute.out`（发布回执） |
| Subject（出） | `agent.feedback.out`（event）；心跳 `agent.feedback.hb`（周期 ≤5s） |
| 出向动作 | `feedback.status`：`warning_id` / `reach_stats` / `response_state` / `effect_score` |
| 形态 | 以 event 主动上报为主（无需平台 request 触发） |

## 数据访问

只读走平台数据 API（`GET /api/v1/warnings`、`GET /api/v1/tasks`、telemetry）。

## 平台侧降级（已实现，供对照）

链路第 ⑤ 段（feedback）汇总触达回执并发布 `ops.feedback.status`；回执统计落库。
效果评估（`effect_score` 的智能侧计算）目前只有回执统计口径，等本智能体交付。

## 与阈值标定闭环的关系

平台侧规划：准确率回放（`GET /api/v1/accuracy/replay`）产出分规则命中/误报对照 →
预警规则库版本修订建议（人工审核生效）。反馈智能体的 `effect_score` 可作为
"这条预警是否有效"的佐证信号进入同一闭环——字段语义在联调时对齐。

## 参考

- 行为样板：`backend/src/aegis/agents/mock.py`（feedback 部分）
- 门禁与接入流程：见 [`agents/README.md`](../README.md)
- 完整规范：[`contracts/AGENT_INTEGRATION_SPEC.v1.md`](../../contracts/AGENT_INTEGRATION_SPEC.v1.md)

## 交付记录（交付时填写）

- 交付方 / 日期 / 版本：
- 实现说明与运行方式：
- 门禁结果（7 项 + 端到端）：
