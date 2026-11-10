"""
数据质量校验工具（灾种无关，必需字段/物理范围/分类取值由注册表驱动）

【本文件角色】
  - 检查数据完整性（必需字段是否齐全）
  - 检查数值异常（超出物理合理范围）
  - 检查分类字段取值（不在 allowed 集合 → 异常）
  - 可选的数据过期检查（max_age_hours 未配置时不启用）
  输出质量报告，不做修复，不做风险判断。
  ★ valid_ranges 是"物理合理范围"（数据质量口径），【不是】灾害风险阈值。

【对外契约】
  data_validate_tool(processed_data, required_fields, valid_ranges,
                     allowed_values=None, max_age_hours=None, as_of=None) -> 质量报告 dict
  提供给：data_validate_node.py
"""

from datetime import UTC, datetime


def data_validate_tool(
    processed_data: dict,
    required_fields: list,
    valid_ranges: dict,
    allowed_values: dict | None = None,
    max_age_hours: float | None = None,
    as_of: datetime | None = None,
) -> dict:
    """检查数据质量（灾种无关）。

    参数:
        processed_data: 标准化后的数据
        required_fields: 必需字段清单（来自 hazard_data_registry）
        valid_ranges: {字段名: (最小值, 最大值)} 数值型物理合理范围
        allowed_values: {字段名: [允许取值]} 分类字段合法集合（来自 hazard_data_registry）
        max_age_hours: 数据过期阈值（小时）；None 表示不启用过期检查（Mock 数据时间戳固定）
        as_of: 过期检查的基准时间；None 时使用当前 UTC 时间

    返回:
        质量报告 dict，含 is_valid / completeness_ratio / validation_reason /
        expired_records（启用过期检查时）等
    """
    events = processed_data.get("events", [])
    total_count = len(events)

    quality_report = {
        "total_records": total_count,
        "complete_records": 0,
        "incomplete_records": 0,
        "missing_fields_summary": {},
        "anomalies": [],
        "completeness_ratio": 0.0,
        "is_valid": False,
        "expired_records": 0,
    }

    # ---------- 逐记录检查 ----------
    for event in events:
        # 1) 完整性
        missing = [f for f in required_fields if event.get(f) is None]
        if not missing:
            quality_report["complete_records"] += 1
        else:
            quality_report["incomplete_records"] += 1
            for f in missing:
                quality_report["missing_fields_summary"][f] = quality_report["missing_fields_summary"].get(f, 0) + 1

        # 2) 数值型物理范围
        for field, (low, high) in (valid_ranges or {}).items():
            value = event.get(field)
            if value is not None:
                try:
                    if not (low <= float(value) <= high):
                        quality_report["anomalies"].append(
                            {
                                "event_id": event.get("event_id"),
                                "field": field,
                                "value": value,
                                "reason": f"超出合理范围 [{low}, {high}]",
                            }
                        )
                except (TypeError, ValueError):
                    quality_report["anomalies"].append(
                        {
                            "event_id": event.get("event_id"),
                            "field": field,
                            "value": value,
                            "reason": "无法转换为数值",
                        }
                    )

        # 3) 分类字段取值集合
        for field, allowed in (allowed_values or {}).items():
            value = event.get(field)
            if value is not None and value not in allowed:
                quality_report["anomalies"].append(
                    {
                        "event_id": event.get("event_id"),
                        "field": field,
                        "value": value,
                        "reason": f"分类取值不在允许集合 {allowed} 内",
                    }
                )

        # 4) 数据过期检查（可选；Mock 数据时间戳固定，默认不启用）
        if max_age_hours is not None:
            time_str = event.get("time")
            if time_str:
                try:
                    t = datetime.fromisoformat(str(time_str).replace("Z", "+00:00"))
                    now = as_of or datetime.now(UTC)
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=UTC)
                    age_hours = (now - t).total_seconds() / 3600.0
                    if age_hours > max_age_hours:
                        quality_report["expired_records"] += 1
                        quality_report["anomalies"].append(
                            {
                                "event_id": event.get("event_id"),
                                "field": "time",
                                "value": str(time_str),
                                "reason": f"数据过期：记录时间距今 {age_hours:.1f} 小时，超过阈值 {max_age_hours} 小时",
                            }
                        )
                except (TypeError, ValueError):
                    pass  # 时间无法解析已在标准化环节标记 time_valid=False

    # ---------- 完整性比率与合格判定 ----------
    if total_count > 0:
        quality_report["completeness_ratio"] = round(quality_report["complete_records"] / total_count, 4)

    COMPLETENESS_THRESHOLD = 0.8  # 阈值可配置化（当前为 Mock 约定，无规范来源）
    MAX_ANOMALY_RATIO = 0.1

    anomaly_ratio = len(quality_report["anomalies"]) / max(total_count, 1)

    quality_report["is_valid"] = quality_report["completeness_ratio"] >= COMPLETENESS_THRESHOLD and anomaly_ratio <= MAX_ANOMALY_RATIO

    quality_report["validation_reason"] = (
        f"完整性: {quality_report['completeness_ratio']:.1%} "
        f"(阈值 {COMPLETENESS_THRESHOLD:.0%}), "
        f"异常率: {anomaly_ratio:.1%} (阈值 {MAX_ANOMALY_RATIO:.0%})"
        + (f", 过期记录: {quality_report['expired_records']} 条" if max_age_hours is not None else "")
        + "（阈值为 Mock 约定，无规范来源）"
    )

    return quality_report
