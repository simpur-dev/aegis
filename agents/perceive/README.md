# 感知智能体（perceive）

多源遥测的异常检测与灾害触发条件识别——指标 1"识别 ≥5 类灾害触发条件"的智能体侧贡献。

## 契约要点

| 项 | 值 |
| --- | --- |
| Subject（入） | `agent.perceive.in`（平台 request）；实时流订阅 `data.>`（不查询数据库） |
| Subject（出） | `agent.perceive.out`（event）；心跳 `agent.perceive.hb`（周期 ≤5s） |
| 出向动作 | `perceive.anomaly`：`station_id` / `metric` / `value` / `observed_at` / `anomaly_score`（弱信号与越限标注）<br>`perceive.trigger_hit`：`rule_id` / `hazard_type` / `region_code` / `evidence_refs`（触发条件命中——**指标 1 的溯源依据**，evidence_refs 必须可追溯 to 原始读数） |
| 入向动作 | `assess.hazard`（request，与 assess 智能体共同接收）：`region_code` / `window` / `candidate_hazards` |
| 灾种口径 | `domain/enums.HazardType` 五类：LANDSLIDE / ROCKFALL / DEBRIS_FLOW / AVALANCHE / LAKE_OUTBURST（+UNKNOWN），禁止自定义枚举 |

## 数据访问

只读走平台数据 API（`GET /api/v1/telemetry` 等，服务令牌鉴权）；实时数据只订阅 `data.>`。

## 平台侧降级（已实现，供对照）

智能体缺位/超时/违约时，平台用阈值规则引擎跑触发识别
（`backend/src/aegis/services/trigger_rules.py`：9 条规则 × 5 灾种，纯函数）。
**这意味着你的交付质量决定链路走智能路径还是规则路径**——降级事实会记进
`ChainResult.degradations` 并出现在 `/api/v1/integrations` 与链路 trace 里，按 `agent_id` 归因。

## 参考

- 行为样板：`backend/src/aegis/agents/mock.py`（perceive 部分）
- 门禁：`backend/tests/contract/test_agent_conformance.py`
- 完整规范：[`contracts/AGENT_INTEGRATION_SPEC.v1.md`](../../contracts/AGENT_INTEGRATION_SPEC.v1.md)
- 接入流程与环境：[`agents/README.md`](../README.md)

## 交付记录（交付时填写）

- 交付方 / 日期 / 版本：
- 实现说明与运行方式：
- 门禁结果（7 项 + 端到端）：
