# 研判智能体（assess）

灾种识别与风险等级评估：候选灾种 + 区域窗口 → 定级结论（含可解释依据链）。

## 契约要点

| 项 | 值 |
| --- | --- |
| Subject（入） | `agent.assess.in`（平台 request：`assess.hazard`——`region_code` / `window` / `candidate_hazards`） |
| Subject（出） | `agent.assess.out`（response）；心跳 `agent.assess.hb`（周期 ≤5s） |
| 出向动作 | `assess.risk_level`：`hazard_type` / `region_code` / `risk_level`(1-5) / `confidence` / `rationale` / `evidence_refs` |
| 等级口径 | `risk_level` 1-5 对应红/橙/黄/蓝/无（1 最高），全平台统一，禁止自定义分级 |
| 期限 | request 带 `deadline_ms`；超期由**平台网关**判 `E_TIMEOUT`，智能体不得在超期后二次执行 |

`rationale` 与 `evidence_refs` 不是装饰字段：预警详情页与"认知镜像"（语义助手解释预警）
直接消费它们，缺失会让结论不可解释。

## 数据访问

只读走平台数据 API（telemetry / 图谱检索 / 案例库）；混合检索能力（bge-m3 + BM25 + RRF）
平台已内建，如需语义佐证可走 `GET /api/v1/retrieval/search` 或案例召回端点，不必自建索引。

## 平台侧降级（已实现，供对照）

`backend/src/aegis/services/risk_engine.py`：命中折算 1-5 级、加权得分 + 佐证增益置信度、
可解释 rationale。智能体缺位时链路用它定级。

## 参考

- 行为样板：`backend/src/aegis/agents/mock.py`（assess 部分）
- 门禁与接入流程：见 [`agents/README.md`](../README.md)
- 完整规范：[`contracts/AGENT_INTEGRATION_SPEC.v1.md`](../../contracts/AGENT_INTEGRATION_SPEC.v1.md)

## 交付记录（交付时填写）

- 交付方 / 日期 / 版本：
- 实现说明与运行方式：
- 门禁结果（7 项 + 端到端）：
