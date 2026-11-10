"""灾种能力的请求/响应契约——**定义一次，四方共用**。

四方都读这一份，不再各写各的：

- **适配器**（`*/agent.py`）：按这里的契约造应答；
- **闭环**（`manager_agent/dispatcher.py`）：按这里的契约造请求；
- **mock 替身**（`agents/mock.py`）：按这里的契约造应答；
- **平台**（`pipeline/stages.py`）：按这里的契约读应答。

契约用**普通 dict** 而不是 Pydantic DTO，是因为这些腿的请求要么来自平台既有 DTO
（`AssessRequest`）、要么来自闭环内部的原生形状，两边都不该为了穿过总线先转成
第三个形状。字段约束写在下面，靠 `tests/unit/agents/test_hazard_adapters.py` 守。

字段命名的取舍：凡平台侧要读的名字，一律沿用平台既有口径（`is_valid` /
`completeness_ratio` / `sufficient` / `risk_level` / `confidence` / `rationale` /
`evidence_refs`）；只有闭环内部才消费的键（`processed_data`、`risk_factors`…）才带
原模块的命名，且**只出现在该腿自己的应答里**，不外泄。
"""

from __future__ import annotations

from typing import Any

# ---------- `data_quality`（数据质量门）----------


def with_native(contract: dict[str, Any], native: Any) -> dict[str, Any]:
    """契约应答 + 原生产出（`native` 键）。

    `native` 是该腿**原模块形状**的交付物，只给闭环内部用（下一条腿按原形状写的），
    平台侧不读它。放在同一份应答里而不是另开一条通道，是为了让"这条腿交付了什么"
    始终只有一处可看——分两处迟早对不上。
    """
    return {**contract, "native": native}


def data_quality_request(
    *,
    hazard_type: str,
    region_code: str = "",
    window: str = "",
    evidence: dict[str, Any] | None = None,
    fetch: bool = False,
    task: str = "",
) -> dict[str, Any]:
    """数据质量门请求。

    `evidence` 与 `fetch` **二选一**：有证据包就校验平台推来的证据（生产路径）；
    `fetch=true` 则走本模块自带的取数-清洗-校验链（闭环/演练路径，数据源是 Mock 源）。
    两个都不给就没有数据可判——那是调用方的错，不是本体的错。
    """
    return {
        "hazard_type": hazard_type,
        "region_code": region_code,
        "window": window,
        "evidence": evidence or {},
        "fetch": bool(fetch),
        "task": task,
    }


def data_quality_response(verdict: dict[str, Any]) -> dict[str, Any]:
    """数据质量门应答：固定六个判据字段 + 取数路径才有的交付物。"""
    return {
        "is_valid": bool(verdict.get("is_valid")),
        "completeness_ratio": float(verdict.get("completeness_ratio") or 0.0),
        "validation_reason": str(verdict.get("validation_reason") or ""),
        "sufficient": bool(verdict.get("sufficient")),
        "reason": str(verdict.get("reason") or ""),
        "missing_data": list(verdict.get("missing_data") or [])[:32],
    }


# ---------- `factor_risk_assess`（因子引擎定级）----------


def factor_risk_request(
    *,
    hazard_type: str,
    region_code: str = "",
    window: str = "",
    evidence: dict[str, Any] | None = None,
    data_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """因子引擎请求。`evidence` 与 `data_result` 二选一：

    - `evidence`：平台推送的证据包（生产路径）；
    - `data_result`：闭环内部取数产物（`processed_data.events`，演练路径）。

    两条路径都落到同一套因子观测上——生产路径直接用证据包里的观测，演练路径由本体
    把扁平事件聚合成观测。**本体不会为了让形状好看而伪造平台 DTO**：演练路径的区域
    可以是中文地名，它就不是一个平台区域码，也不该被硬拗成。
    """
    return {
        "hazard_type": hazard_type,
        "region_code": region_code,
        "window": window,
        "evidence": evidence or {},
        "data_result": data_result or {},
    }


def factor_risk_response(assessment: dict[str, Any]) -> dict[str, Any]:
    """因子引擎应答。平台侧只读定级相关的几个字段，闭环侧还读 `status` 与因子明细。"""
    return {
        "hazard_type": str(assessment.get("hazard_type") or ""),
        "region_code": str(assessment.get("region_code") or ""),
        "risk_level": int(assessment.get("risk_level") or 0),
        "confidence": float(assessment.get("confidence") or 0.0),
        "confidence_basis": str(assessment.get("confidence_basis") or ""),
        "rationale": str(assessment.get("rationale") or ""),
        "evidence_refs": list(assessment.get("evidence_refs") or [])[:32],
        "source": str(assessment.get("source") or ""),
        "assessed_by": str(assessment.get("assessed_by") or ""),
        "human_review_suggested": bool(assessment.get("human_review_suggested")),
        "assessment": dict(assessment.get("assessment") or {}),
        "data_provenance": dict(assessment.get("data_provenance") or {}),
        # 闭环内部消费：下游三腿（仿真/预警/审计）按这两个键判断"这一步成没成"。
        "status": str(assessment.get("status") or "success"),
        "risk_factors": list(assessment.get("risk_factors") or [])[:32],
    }


# ---------- `scenario_sim`（情景仿真）----------


def scenario_sim_request(
    *,
    hazard_type: str,
    data_result: dict[str, Any] | None = None,
    risk_result: dict[str, Any] | None = None,
    task: str = "",
    preset_scenario_configs: list[Any] | None = None,
) -> dict[str, Any]:
    """情景仿真请求。产出全程带 mock 标记，只跑在闭环/演练路径。"""
    return {
        "hazard_type": hazard_type,
        "data_result": data_result or {},
        "risk_result": risk_result or {},
        "task": task,
        "preset_scenario_configs": list(preset_scenario_configs or []),
    }


# ---------- `warn_content`（预警文案素材）----------


def warn_content_request(
    *,
    hazard_type: str,
    risk_result: dict[str, Any] | None = None,
    simulation_result: dict[str, Any] | None = None,
    data_result: dict[str, Any] | None = None,
    warning_id: str = "",
    level: int = 0,
    regions: list[str] | None = None,
) -> dict[str, Any]:
    """预警文案素材请求。

    `risk_result` 两种形状都收：平台的定级结论（`risk_level`/`rationale`）与闭环内部的
    原生风险 dict（`assessment`/`risk_level`）。本体按可用字段归一，**不替调用方编等级**。
    """
    return {
        "hazard_type": hazard_type,
        "risk_result": risk_result or {},
        "simulation_result": simulation_result or {},
        "data_result": data_result or {},
        "warning_id": warning_id,
        "level": int(level or 0),
        "regions": list(regions or []),
    }


def warn_content_response(draft: dict[str, Any]) -> dict[str, Any]:
    """预警文案素材应答。

    **只给素材，不给发布动作**：应答里没有"已发布"、没有通道回执、没有官方等级。
    发布权在平台执行内环（受众分群、防幻觉数字闸、藏文），触达证据只认平台交付腿的
    真回执——这条链路的规矩不能因为多了一条腿就松。
    """
    return {
        "content_only": True,
        "explanation_text": str(draft.get("explanation_text") or ""),
        "suggested_actions": list(draft.get("suggested_actions") or [])[:16],
        "cap_xml": str(draft.get("cap_xml") or ""),
        "cap_dict": dict(draft.get("cap_dict") or {}),
        "based_on": list(draft.get("based_on") or [])[:16],
        "warning_level": str(draft.get("warning_level") or ""),
        "target_region": str(draft.get("target_region") or ""),
        "is_mock": bool(draft.get("is_mock")),
    }


# ---------- `chain_audit`（链内一致性审计）----------


def chain_audit_request(
    *,
    hazard_type: str,
    data_result: dict[str, Any] | None = None,
    risk_result: dict[str, Any] | None = None,
    simulation_result: dict[str, Any] | None = None,
    warning_result: dict[str, Any] | None = None,
    event_id: str = "",
    warning_id: str = "",
) -> dict[str, Any]:
    """链内一致性审计请求。审计的是**链路自己有没有自相矛盾**，不是触达效果。"""
    return {
        "hazard_type": hazard_type,
        "data_result": data_result or {},
        "risk_result": risk_result or {},
        "simulation_result": simulation_result or {},
        "warning_result": warning_result or {},
        "event_id": event_id,
        "warning_id": warning_id,
    }


# ---------- `hazard_workflow`（灾种闭环入口）----------


def hazard_workflow_request(
    *,
    hazard_type: str,
    task_type: str = "full_analysis",
    region: str = "",
    user_request: str = "",
    event_id: str = "",
    analysis_time: str = "",
    require_simulation: bool = False,
    preset_scenario_configs: list[Any] | None = None,
) -> dict[str, Any]:
    """灾种闭环入口请求。"""
    return {
        "hazard_type": hazard_type,
        "task_type": task_type,
        "region": region,
        "user_request": user_request,
        "event_id": event_id,
        "analysis_time": analysis_time,
        "require_simulation": bool(require_simulation),
        "preset_scenario_configs": list(preset_scenario_configs or []),
    }


__all__ = [
    "chain_audit_request",
    "data_quality_request",
    "data_quality_response",
    "factor_risk_request",
    "factor_risk_response",
    "hazard_workflow_request",
    "scenario_sim_request",
    "warn_content_request",
    "warn_content_response",
    "with_native",
]
