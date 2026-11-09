"""Risk Agent 的旧入口（events 口径）——**仅供未接线的 manager 内部使用，待退役**。

平台的研判路径已经统一走 `assess.v1`（见 `main.run_risk_agent`）；这里保留旧形状，
只是因为 `manager_agent` / `data_agent` 还没接线（ADR-0012 记录的已知限制）。
它不是平台适配层：平台路径不引用本模块，任何新代码也不应引用。

本模块把旧 `data_result.processed_data.events`（扁平的灾种事件）转成统一证据包的
`FactorObservation` 列表——`data_kind="unknown"`，如实说明这批数据的出处不可考。
"""

from __future__ import annotations

import json
import sys
from typing import Any

from aegis.agents_ext.hazard_registry import normalize_hazard_type
from aegis.agents_ext.risk_agent.graph import create_risk_agent
from aegis.agents_ext.risk_agent.state import RiskAgentState
from aegis.agents_ext.risk_agent.tools.hazard_risk_registry import get_hazard_risk_profile
from aegis.domain.provenance import DataProvenance

_TELEMETRY_KEYS = frozenset({"event_id", "time", "latitude", "longitude", "region", "source", "hazard_type"})


def _observations_from_events(events: list, region: str) -> list[dict]:
    """旧 events → `FactorObservation` dump。同名字段跨事件合并成一个窗口聚合。"""
    numeric: dict[str, list[float]] = {}
    classes: dict[str, str] = {}
    latest: dict[str, str] = {}
    total: dict[str, int] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        stamp = str(event.get("time") or "")
        for key, value in event.items():
            if key in _TELEMETRY_KEYS or value is None:
                continue
            total[key] = total.get(key, 0) + 1
            latest[key] = max(latest.get(key, ""), stamp)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                numeric.setdefault(key, []).append(float(value))
            else:
                classes.setdefault(key, str(value))

    rows: list[dict] = []
    for key in total:
        values = numeric.get(key, [])
        row: dict[str, Any] = {
            "factor": key,
            "samples": total[key],
            "observed_at": latest.get(key) or "unknown",
            "quality": "partial",
            "sources": ["legacy_events"],
            "derivation": "legacy_data_result",
        }
        if values:
            row["min_value"] = min(values)
            row["max_value"] = max(values)
            row["mean_value"] = sum(values) / len(values)
        elif key in classes:
            row["class_value"] = classes[key]
        else:
            continue
        rows.append(row)
    return rows


def run_risk_agent_legacy(task: str, data_result: dict, hazard_type: str, max_retries: int = 3) -> dict:
    """旧形状入口：返回 Risk 最终 `risk_result` dict（未接线的 manager 读它）。"""
    hazard = normalize_hazard_type(hazard_type) or hazard_type
    events = (data_result.get("processed_data") or {}).get("events") or []
    region = str(events[0].get("region", "unknown")) if events and isinstance(events[0], dict) else "unknown"
    provenance = DataProvenance(
        data_kind="unknown",
        sources=["legacy_events"],
        method="unknown",
        is_mock=bool((data_result.get("processed_data") or {}).get("is_mock")),
        disclaimer="legacy 入口（未接线 manager 内部使用），数据出处不可考",
    )
    initial_state: RiskAgentState = {
        "task": task,
        "hazard_type": hazard,
        "region_code": region,
        "observations": _observations_from_events(events, region),
        "site_profile": None,
        "data_provenance": provenance.model_dump(mode="json"),
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
    final_state = create_risk_agent().invoke(initial_state)
    return final_state.get("risk_result", {})


def _mock_data_result(hazard_type: str = "debris_flow", complete: bool = True) -> dict:
    """构造旧口径的 Mock data_result（仅供本模块自测与未接线 manager 联调）。"""
    profile = get_hazard_risk_profile(hazard_type)
    events = []
    for i in range(1, 3):
        event = {
            "event_id": f"MOCK-{profile['hazard_type_cn']}-{i:03d}",
            "time": "2024-07-08T03:00:00Z",
            "latitude": 29.6 + i * 0.01,
            "longitude": 95.3 + i * 0.01,
            "region": "西藏",
            "source": "mock",
        }
        for field, value in profile.get("sample_values", {}).items():
            event[field] = value if complete else None
        events.append(event)
    return {
        "processed_data": {"events": events, "data_source": "mock", "hazard_type": profile["hazard_type"], "is_mock": True},
        "data_quality": {"is_valid": True, "completeness_ratio": 1.0, "validation_reason": "Mock 合格"},
        "missing_data": [],
        "status": "success",
        "error_message": "",
        "hazard_type": profile["hazard_type"],
    }


if __name__ == "__main__":
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8")
        except Exception:
            pass
    result = run_risk_agent_legacy("评估泥石流风险", _mock_data_result("debris_flow"), hazard_type="debris_flow")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


__all__ = ["run_risk_agent_legacy"]
