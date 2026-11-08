# 决策智能体（plan）

防控任务智能解析与自动化拆解：风险结论 + 目标约束 → 标准化任务单元集（STU）；异常工况重调度建议。

## 契约要点

| 项 | 值 |
| --- | --- |
| Subject（入） | `agent.plan.in`（request） |
| Subject（出） | `agent.plan.out`（response）；心跳 `agent.plan.hb`（周期 ≤5s） |
| 入向动作 | `plan.stu`：`event_id` / `risk` / `objective` / `constraints`<br>`plan.reschedule`：`workflow_instance_id` / `anomaly` / `current_topology`（**重调度建议须在 10s 内**，对应考核指标） |
| 出向动作 | `plan.stu_result`：`task_units`（STU 数组，**逐条过 `stu.v1.schema.json` 校验，不合契约即被网关拒收**） |
| 失败回执 | 内部异常回 `error.raise`（`code` / `message` / `retryable`），不得静默 |

STU 的字段口径（任务 ID / 类型 / 区域 / 优先级 / 责任人 / 时限 / 前置依赖）以
`contracts/stu.v1.schema.json` 为唯一真源；`fallback_policy` 字段决定平台在拆解失败时的兜底策略。

## 数据访问

只读走平台数据 API；预案参考可用案例召回（`GET /api/v1/cases/recall`）与
`GET /api/v1/tasks`（历史任务单元）。

## 平台侧降级（已实现，供对照）

`backend/src/aegis/services/task_parser.py`：灾种 Playbook 剧本拆解
（5 灾种 × 核查/预警/转移/监测/报告步骤矩阵，低等级不启动转移）。
智能体缺位/超时/产出不合 Schema 时，平台用它生成 STU——链路不断，但"智能拆解"退化为固定剧本。

## 参考

- 行为样板：`backend/src/aegis/agents/mock.py`（plan 部分）
- 门禁与接入流程：见 [`agents/README.md`](../README.md)
- 完整规范：[`contracts/AGENT_INTEGRATION_SPEC.v1.md`](../../contracts/AGENT_INTEGRATION_SPEC.v1.md)

## 交付记录（交付时填写）

- 交付方 / 日期 / 版本：
- 实现说明与运行方式：
- 门禁结果（7 项 + 端到端）：
