"""平台推送证据包的质量校验（灾种无关）。

研判段在派单之前先问一句"这份数据够不够研判"：够才值得花一次 LLM 调用，不够就把
理由留在台账上、让降级路径接手——而不是照研判一遍再发现数据根本不够。

这份判据与"数据异常检测"是两件事：这里判的是**证据包本身完不完整**，不是"读数越没
越限"。越限判断归感知段。
"""

from __future__ import annotations

from typing import Any

#: 完整度阈值：低于它就不算合格证据包。与 `data_validate_tool` 的口径一致
#: （0.8 是 Mock 阶段的约定值，尚无规范来源——有规范来源前，这里显式承认它是约定）。
COMPLETENESS_THRESHOLD = 0.8

#: 一条观测被认为"可用"的最低条件：数值或类别值至少有一个不是 None。
#: 平台侧的 `FactorObservation` 已经在建模层保证了这一点，这里是防御式复核——
#: 证据包来自总线，形状不该假设永远正确。
_VALUE_KEYS = ("min_value", "max_value", "mean_value", "class_value")


def _is_usable(observation: dict[str, Any]) -> bool:
    return any(observation.get(key) is not None for key in _VALUE_KEYS)


def evidence_quality_tool(
    evidence: dict[str, Any],
    *,
    region_code: str = "",
    completeness_threshold: float = COMPLETENESS_THRESHOLD,
) -> dict[str, Any]:
    """校验一份证据包，返回研判准入判据。

    返回：
        {is_valid, completeness_ratio, validation_reason, sufficient, reason, missing_data[]}

    `is_valid` 是"质量合格"，`sufficient` 是"够不够研判"——两件事：
    一份完整度 60% 但有三条可用观测的包，`is_valid` 为假（不合格），
    但 `sufficient` 为真（还是能研判，只是要在台账上留痕）。
    """
    observations = evidence.get("observations") or []
    total = len(observations)
    usable = [obs for obs in observations if isinstance(obs, dict) and _is_usable(obs)]
    degraded = [obs for obs in observations if isinstance(obs, dict) and obs.get("quality") not in (None, "ok")]

    completeness_ratio = round(len(usable) / total, 4) if total else 0.0

    missing: list[str] = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        factor = str(obs.get("factor") or "unknown")
        if obs not in usable:
            missing.append(f"因子 {factor} 没有可用数值")
        elif obs.get("quality") not in (None, "ok"):
            missing.append(f"因子 {factor} 观测质量 {obs.get('quality')}")
    for unmapped in evidence.get("unmapped_metrics") or []:
        if isinstance(unmapped, dict):
            missing.append(f"未映射指标 {unmapped.get('metric', 'unknown')}")
        else:
            missing.append(f"未映射指标 {unmapped}")

    truncated = bool(evidence.get("truncated"))
    region_ok = not region_code or str(evidence.get("region_code") or "") == region_code

    sufficient = bool(usable) and not truncated and region_ok
    is_valid = sufficient and completeness_ratio >= completeness_threshold

    reasons: list[str] = []
    if total == 0:
        reasons.append("证据包没有任何观测")
    if usable and completeness_ratio < completeness_threshold:
        reasons.append(f"完整度 {completeness_ratio:.1%} 低于阈值 {completeness_threshold:.0%}")
    if truncated:
        reasons.append("证据包被截断，窗口内可能有因子没进来")
    if not region_ok:
        reasons.append(f"证据区域 {evidence.get('region_code')} 与请求区域 {region_code} 不一致")
    if not reasons:
        reasons.append(f"完整度 {completeness_ratio:.1%}，{len(usable)}/{total} 条观测可用")

    validation_reason = "；".join(reasons)
    return {
        "is_valid": is_valid,
        "completeness_ratio": completeness_ratio,
        "validation_reason": validation_reason,
        "sufficient": sufficient,
        "reason": validation_reason,
        "missing_data": missing[:32],
        "total_observations": total,
        "usable_observations": len(usable),
        "degraded_observations": len(degraded),
    }


__all__ = ["COMPLETENESS_THRESHOLD", "evidence_quality_tool"]
