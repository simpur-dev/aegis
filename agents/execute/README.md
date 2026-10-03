# 执行智能体（execute）

预警发布与联动执行：接收发布指令，多通道下发，回传逐通道回执。

## 契约要点

| 项 | 值 |
| --- | --- |
| Subject（入） | `agent.execute.in`（request：`execute.warn`——`warning_id` / `level` / `regions` / `audiences` / `content_refs`） |
| Subject（出） | `agent.execute.out`（response）；心跳 `agent.execute.hb`（周期 ≤5s） |
| 出向动作 | `execute.ack`：`warning_id` / `channel_results`（每通道 `delivered` / `pending` / `failed`）——**"靶向触达 ≤20min"的量测点就是这条回执** |
| 期限 | request 带 `deadline_ms`；超期由平台网关判 `E_TIMEOUT` |

## 数据访问

只读走平台数据 API（预警内容经 `content_refs` 引用取回，不内嵌大载荷）。

## 平台侧降级（已实现，供对照）

`backend/src/aegis/services/delivery.py`：通道矩阵按预警等级路由（红色含北斗）、
Mock 通道适配器（模拟时延/回执/重试）与回执落库（`warning_receipts`）。
智能体在场时投递由智能体完成后回执给平台，平台侧不再走自己的通道埋点
（两种模式的触达时延口径不同，横向比较要用同一模式——见 `docs/REPORT.md` 口径提醒）。

## 参考

- 行为样板：`backend/src/aegis/agents/mock.py`（execute 部分）
- 门禁与接入流程：见 [`agents/README.md`](../README.md)
- 完整规范：[`contracts/AGENT_INTEGRATION_SPEC.v1.md`](../../contracts/AGENT_INTEGRATION_SPEC.v1.md)

## 交付记录（交付时填写）

- 交付方 / 日期 / 版本：
- 实现说明与运行方式（通道清单：短信/广播/……，凭据如何注入）：
- 门禁结果（7 项 + 端到端）：
