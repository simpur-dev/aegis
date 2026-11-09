"""
数据清洗工具（灾种无关，字段由注册表驱动）

【本文件角色】
  - 按 event_id 去重
  - 标记缺失值（不删除，留给校验判断）
  必填字段清单由调用方（data_clean_node，按灾种注册表）传入，
  本工具不含任何灾种专属字段。

【对外契约】
  data_clean_tool(raw_data, required_fields) ->
      {"events": [...], "cleaning_report": {...}, "data_source": ...}
  提供给：data_clean_node.py
"""


def data_clean_tool(raw_data: dict, required_fields: list) -> dict:
    """清洗原始观测数据（灾种无关）。

    参数:
        raw_data: 灾种数据源工具返回的原始数据（含 events 列表）
        required_fields: 参与缺失标记的字段清单
                         （= 公共必需字段 + 灾种必需观测字段，来自 hazard_data_registry）

    返回:
        {"events": [...], "cleaning_report": {...}, "data_source": ...}
    """
    events = raw_data.get("events", [])
    cleaned_events = []
    cleaning_report = {
        "total_input": len(events),
        "duplicates_removed": 0,
        "null_fields_found": [],
        "total_output": 0,
    }

    # 1. 去重（基于 event_id，与灾种无关）
    seen_ids = set()
    unique_events = []
    for event in events:
        eid = event.get("event_id")
        if eid and eid not in seen_ids:
            seen_ids.add(eid)
            unique_events.append(event)
        elif eid in seen_ids:
            cleaning_report["duplicates_removed"] += 1

    # 2. 检查并标记缺失值（不直接删除，留给后续判断）
    for event in unique_events:
        missing_fields = []
        for field in required_fields:
            if event.get(field) is None:
                missing_fields.append(field)

        if missing_fields:
            cleaning_report["null_fields_found"].append(
                {
                    "event_id": event.get("event_id", "unknown"),
                    "missing_fields": missing_fields,
                }
            )

        event["_has_missing"] = len(missing_fields) > 0
        cleaned_events.append(event)

    cleaning_report["total_output"] = len(cleaned_events)

    return {
        "events": cleaned_events,
        "cleaning_report": cleaning_report,
        "data_source": raw_data.get("data_source", "unknown"),
    }
