"""
Risk Agent 的**权威入口**：吃统一 `assess.v1` 契约，吐 `AssessResponse`。

【这一层做什么】
  只做"内部图结果 → 契约响应"的一次性收口：灾种解析、状态四态 → 错误码、
  临时等级 → 平台 RiskLevel、置信度/出处/证据引用的组装。平台侧因此不再认识
  "外部结果 dict"，也不需要任何适配层。

【灾种显式必填】
  `request.hazard_type` 优先；缺失时取 `hits` 里第一条灾种，仍取不到就诚实拒绝
  （`E_ASSESS_INSUFFICIENT`），绝不让 LLM 猜测灾种。地震/未知灾种 →
  `E_ASSESS_HAZARD_UNSUP`。

【诚实声明】
  五灾种风险规则全部为 MOCK 玩具规则（MOCK-v0.1），R 为原型候选公式；
  响应恒带 `data_provenance.is_mock=True`、`human_review_suggested=True`、
  置信度封顶 0.60。输出不是真实地质灾害风险预测。

【对外契约】
  run_risk_agent(request, evidence, *, agent_id=..., max_retries=3) -> AssessResponse
  失败抛 `AssessRejected`（由 `agent.py` 转成 error 回执）。
  旧 events 口径的入口在 `legacy.py`，仅供未接线的 manager 内部使用，平台路径不引用。
"""

from aegis.agents.hazards.hazard_registry import (
    HAZARD_CN_NAME,
    UnsupportedHazardError,
    require_hazard,
    supported_hazards_text,
)
from aegis.agents.hazards.risk_agent.errors import AssessRejected
from aegis.agents.hazards.risk_agent.graph import create_risk_agent
from aegis.agents.hazards.risk_agent.state import RiskAgentState
from aegis.domain.assess import AssessErrorCode, AssessEvidence, AssessRequest, AssessResponse
from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import now_iso

#: 数据出处标记：外部实现的数据/权重是 Mock 源，读的人一眼能看出。
SOURCE_MARK = "agents_ext_mock"

#: 临时等级（MOCK 阈值）→ 平台 RiskLevel。`none` 是"算出来没风险"；`unknown` 不进表
#: （那是"算不出来"，不能写成 5=无风险）。
_LEVEL_FROM_PROVISIONAL = {"high": RiskLevel.RED, "medium": RiskLevel.ORANGE, "low": RiskLevel.YELLOW, "none": RiskLevel.NONE}

_MAX_EVIDENCE_REFS = 32


def _resolve_hazard(request: AssessRequest) -> str:
    """请求显式灾种优先，其次第一条命中携带的灾种；取不到/不支持就拒绝。"""
    raw = request.hazard_type.value if request.hazard_type is not None else ""
    if not raw:
        raw = next((hit.hazard_type for hit in request.hits if hit.hazard_type), "")
    if not raw:
        raise AssessRejected(
            AssessErrorCode.INSUFFICIENT,
            "研判请求未提供灾种（hazard_type 与 hits 均为空），无法选择评估规则",
            detail={"supported": supported_hazards_text()},
        )
    try:
        return require_hazard(raw)
    except UnsupportedHazardError as exc:
        raise AssessRejected(
            AssessErrorCode.HAZARD_UNSUP,
            str(exc),
            detail={"hazard_type": raw, "supported": supported_hazards_text()},
        ) from exc


def _evidence_refs(request: AssessRequest, result: dict) -> list[str]:
    """证据引用：请求给的证据快照 + 命中的规则 + 实际用到的因子（去重、截断 32）。"""
    refs: list[str] = list(request.data_refs)
    assessment = result.get("assessment", {}) or {}
    refs.extend(f"rule:{hit['rule_id']}" for hit in assessment.get("rule_hits", []) if isinstance(hit, dict) and hit.get("rule_id"))
    refs.extend(f"factor:{key}" for key, spec in (assessment.get("factor_scores") or {}).items() if not spec.get("missing"))
    return list(dict.fromkeys(refs))[:_MAX_EVIDENCE_REFS]


def _initial_state(request: AssessRequest, evidence: AssessEvidence, hazard: str, *, max_retries: int) -> RiskAgentState:
    return {
        "task": f"研判 {request.region_code} 的{HAZARD_CN_NAME.get(hazard, hazard)}风险",
        "hazard_type": hazard,
        "region_code": request.region_code,
        "observations": [item.model_dump(mode="json") for item in evidence.observations],
        "site_profile": evidence.site_profile.model_dump(mode="json") if evidence.site_profile is not None else None,
        "data_provenance": evidence.provenance.model_dump(mode="json"),
        "max_retries": max_retries,
        "risk_factors": [],
        "calculation_result": {},
        "risk_result": {},
        "evidence": [],
        "uncertainties": [],
        "validation_result": {},
        "retry_count": 0,
        "next": "",
        "error_message": "",
        "status": "",
    }


def run_risk_agent(
    request: AssessRequest,
    evidence: AssessEvidence,
    *,
    agent_id: str = "assess.agents_ext",
    max_retries: int = 3,
) -> AssessResponse:
    """统一契约入口：`AssessRequest` + 证据包 → `AssessResponse`；给不出结论抛 `AssessRejected`。"""
    hazard = _resolve_hazard(request)
    final_state = create_risk_agent().invoke(_initial_state(request, evidence, hazard, max_retries=max_retries))
    result = final_state.get("risk_result", {}) or {}
    status = str(final_state.get("status") or result.get("status") or "")

    if status == "insufficient_data":
        reasons = result.get("assessment", {}).get("insufficient_reasons") or result.get("missing_info") or []
        raise AssessRejected(
            AssessErrorCode.INSUFFICIENT,
            f"数据不足以支撑{HAZARD_CN_NAME.get(hazard, hazard)}研判：" + "；".join(str(r) for r in reasons)[:300],
            retryable=False,
            detail={"hazard_type": hazard, "missing_info": list(result.get("missing_info", []))[:16]},
        )
    if status == "failed":
        raise AssessRejected(
            AssessErrorCode.FAILED,
            f"研判执行失败：{result.get('reasoning') or '内部验证未通过'}"[:300],
            retryable=True,
            detail={"hazard_type": hazard},
        )
    if status != "success":
        raise AssessRejected(
            AssessErrorCode.UNCERTAIN,
            f"研判未能形成可采纳结论（status={status or '空'}）：{result.get('reasoning') or ''}"[:300],
            retryable=True,
            detail={"hazard_type": hazard, "validation": result.get("validation", {})},
        )

    provisional = str(result.get("risk_level") or "")
    level = _LEVEL_FROM_PROVISIONAL.get(provisional)
    if level is None:
        # "算不出来"不能落成平台 5（无风险）：宁可拒绝，也不做假定级。
        raise AssessRejected(
            AssessErrorCode.BAD_LEVEL,
            f"研判档位无法映射到平台等级：{provisional!r}",
            detail={"hazard_type": hazard, "provisional_level": provisional},
        )

    return AssessResponse(
        hazard_type=HazardType(hazard),
        region_code=request.region_code,
        risk_level=level,
        confidence=float(result.get("confidence", 0.0)),
        confidence_basis=str(result.get("confidence_basis") or ""),
        rationale=str(result.get("reasoning") or "")[:1000],
        evidence_refs=_evidence_refs(request, result),
        assessed_at=now_iso(),
        assessed_by=agent_id,
        # mock 方法是恒定的：MOCK 权重 + 原型公式，任何一条结论都建议人工核签。
        human_review_suggested=True,
        double_blind="not_applicable",
        double_blind_mode="",
        source=SOURCE_MARK,
        data_provenance=result.get("data_provenance", {}),
        assessment=result.get("assessment", {}),
    )


__all__ = ["SOURCE_MARK", "run_risk_agent"]
