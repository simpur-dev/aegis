"""
CAP 1.2 格式转换工具

【本文件角色】
  将 Warning Agent 已校验的内部预警对象映射为 OASIS CAP 1.2 数据模型，
  做结构校验并序列化为 XML。只做"格式映射与校验"，
  【不是】风险评估或预警决策模型，也不因生成 CAP 而声称满足真实业务发布要求。

【★ 四个概念严格区分（不得混用）★】
  - warning_level：项目内部等级（none/advisory/watch/warning，MOCK 映射）
  - CAP urgency   ：响应紧迫度（Immediate/Expected/Future/Past/Unknown）
  - CAP severity  ：事件严重度（Extreme/Severe/Moderate/Minor/Unknown）
  - CAP certainty ：事件确定性（Observed/Likely/Possible/Unlikely/Unknown）
  urgency/severity/certainty 由下方 MOCK 映射表得到（mapping_version=MOCK-v0.1），
  与 warning_level 不是同一概念，也不互相推导。

【诚实声明】
  - 内部预警对象 mock_only=True → CAP status 固定为 "Exercise"（演练），
    event 名称带"Mock 模拟"字样，alert.note 携带非真实预警声明；
  - 本工具不做 XSD/Schema 级验证，只做必需字段与枚举的结构检查；
  - 序列化结果仅供研究原型演示。

【对外契约】
  internal_alert_to_cap(alert, explanation_text=None) -> dict
  validate_cap_dict(cap) -> list[str]（空列表=通过）
  serialize_cap_xml(cap) -> str
  提供给：warning_output_node（阶段 7 接线）、测试。
"""

import xml.etree.ElementTree as ET

CAP_MAPPING_VERSION = "MOCK-v0.1"

# CAP 枚举（OASIS CAP 1.2）
_CAP_URGENCY = {"Immediate", "Expected", "Future", "Past", "Unknown"}
_CAP_SEVERITY = {"Extreme", "Severe", "Moderate", "Minor", "Unknown"}
_CAP_CERTAINTY = {"Observed", "Likely", "Possible", "Unlikely", "Unknown"}
_CAP_STATUS = {"Actual", "Exercise", "System", "Test", "Draft"}
_CAP_MSG_TYPE = {"Alert", "Update", "Cancel", "Ack", "Error"}

# 内部 warning_level → CAP (urgency, severity, certainty) 的 MOCK 映射表
_LEVEL_TO_CAP = {
    "warning": {"urgency": "Immediate", "severity": "Severe", "certainty": "Unknown"},
    "watch": {"urgency": "Expected", "severity": "Moderate", "certainty": "Unknown"},
    "advisory": {"urgency": "Future", "severity": "Minor", "certainty": "Unknown"},
}


def internal_alert_to_cap(alert: dict, explanation_text: str | None = None) -> dict:
    """把内部预警对象映射为 CAP 1.2 数据模型（dict）。

    mock_only=True → status="Exercise"（演练），event 名称带 "Mock 模拟" 字样。
    内部 warning_level=none → 不生成 CAP（返回 {"status": "skipped", "reason": ...}）。
    """
    if alert.get("warning_level") in (None, "none"):
        return {"status": "skipped", "reason": f"warning_level={alert.get('warning_level')}，不构成 CAP 消息"}

    level = alert["warning_level"]
    mapping = _LEVEL_TO_CAP.get(level, {"urgency": "Unknown", "severity": "Unknown", "certainty": "Unknown"})
    cn = alert.get("hazard_type_cn", alert.get("hazard_type", ""))
    mock_only = alert.get("mock_only", True)

    cap_status = "Exercise" if mock_only else "Actual"
    event_name = f"{cn} Mock 模拟预警" if mock_only else f"{cn} 预警"

    actions = alert.get("suggested_actions", []) or []
    description_parts = [explanation_text] if explanation_text else []
    description_parts.append(
        "【重要】本消息为 Mock 模拟产物（status=Exercise），非真实预警，禁止用于实际发布或应急决策。"
        if mock_only
        else "本消息来自正式预警流程。"
    )

    info = {
        "language": "zh-CN",
        "category": ["Geo"],  # 地质灾害
        "event": event_name,
        "urgency": mapping["urgency"],
        "severity": mapping["severity"],
        "certainty": mapping["certainty"],
        "headline": f"{cn} {level}（Mock 模拟）",
        "description": "\n".join(p for p in description_parts if p),
        "instruction": "；".join(actions) if actions else "（无建议行动）",
        "responseType": ["Prepare"].copy() if level in ("watch", "advisory") else ["Avoid"].copy(),
    }
    # 避免 list 副本共享
    info["responseType"] = list(info["responseType"])

    cap = {
        "identifier": alert.get("alert_id", ""),
        "sender": alert.get("sender", ""),
        "sentTime": alert.get("generated_at", ""),
        "status": cap_status,
        "msgType": "Alert",  # 第一版只有首次告警；Update/Cancel 预留
        "source": f"西藏山地灾害多智能体原型系统 (mapping={CAP_MAPPING_VERSION})",
        "scope": "Public",
        "note": ("Mock 模拟产物，非真实预警，禁止用于实际发布或应急决策" if mock_only else ""),
        "info": [info],
        "area": [{"areaDesc": alert.get("target_region", "未标注区域")}],
        "_mock_only": mock_only,  # 内部标记（序列化时不输出）
        "_mapping_version": CAP_MAPPING_VERSION,
    }
    return cap


def validate_cap_dict(cap: dict) -> list:
    """结构校验：返回问题列表（空列表 = 通过）。检查必需字段与 CAP 枚举。"""
    issues = []
    if not isinstance(cap, dict):
        return ["CAP 数据必须是 dict"]
    for field in ("identifier", "sender", "sentTime", "status", "msgType", "scope"):
        if not cap.get(field):
            issues.append(f"缺少 CAP 必需字段: <{field}>")
    if cap.get("status") not in _CAP_STATUS:
        issues.append(f"status '{cap.get('status')}' 不在 CAP 枚举 {sorted(_CAP_STATUS)} 内")
    if cap.get("msgType") not in _CAP_MSG_TYPE:
        issues.append(f"msgType '{cap.get('msgType')}' 不在 CAP 枚举 {sorted(_CAP_MSG_TYPE)} 内")

    infos = cap.get("info", [])
    if not isinstance(infos, list) or not infos:
        issues.append("缺少 <info> 块（至少一个）")
        return issues
    for i, info in enumerate(infos):
        if not isinstance(info, dict):
            issues.append(f"info[{i}] 必须是 dict")
            continue
        for field in ("language", "event", "urgency", "severity", "certainty", "headline", "description"):
            if not info.get(field):
                issues.append(f"info[{i}] 缺少必需字段: <{field}>")
        if info.get("urgency") not in _CAP_URGENCY:
            issues.append(f"info[{i}] urgency '{info.get('urgency')}' 不在 CAP 枚举内")
        if info.get("severity") not in _CAP_SEVERITY:
            issues.append(f"info[{i}] severity '{info.get('severity')}' 不在 CAP 枚举内")
        if info.get("certainty") not in _CAP_CERTAINTY:
            issues.append(f"info[{i}] certainty '{info.get('certainty')}' 不在 CAP 枚举内")
        for cat in info.get("category", []) or []:
            if cat not in {
                "Geo",
                "Met",
                "Safety",
                "Security",
                "Rescue",
                "Fire",
                "Health",
                "Env",
                "Transport",
                "Utilities",
                "CBRNeEvent",
                "Other",
            }:
                issues.append(f"info[{i}] category '{cat}' 不在 CAP 枚举内")
    return issues


def serialize_cap_xml(cap: dict) -> str:
    """把 CAP dict 序列化为 XML 字符串（不含 XSD 验证，仅供研究原型演示）。

    cap 缺失必需字段时抛 ValueError（由调用方捕获并转化为明确错误信息）。
    """
    issues = validate_cap_dict(cap)
    if issues:
        raise ValueError("CAP 结构校验失败: " + "；".join(issues))

    def _sub(parent, tag, text):
        el = ET.SubElement(parent, tag)
        el.text = str(text)
        return el

    alert_el = ET.Element("alert")
    _sub(alert_el, "identifier", cap["identifier"])
    _sub(alert_el, "sender", cap["sender"])
    _sub(alert_el, "sentTime", cap["sentTime"])
    _sub(alert_el, "status", cap["status"])
    _sub(alert_el, "msgType", cap["msgType"])
    _sub(alert_el, "source", cap.get("source", ""))
    _sub(alert_el, "scope", cap.get("scope", "Public"))
    if cap.get("note"):
        _sub(alert_el, "note", cap["note"])

    for info in cap["info"]:
        info_el = ET.SubElement(alert_el, "info")
        for lang in info.get("category", []):
            _sub(info_el, "category", lang)
        _sub(info_el, "event", info["event"])
        _sub(info_el, "urgency", info["urgency"])
        _sub(info_el, "severity", info["severity"])
        _sub(info_el, "certainty", info["certainty"])
        _sub(info_el, "headline", info["headline"])
        _sub(info_el, "description", info["description"])
        for rt in info.get("responseType", []) or []:
            _sub(info_el, "responseType", rt)
        _sub(info_el, "instruction", info["instruction"])
        area_el = ET.SubElement(info_el, "area")
        for area in cap.get("area", []) or []:
            _sub(area_el, "areaDesc", area.get("areaDesc", ""))

    return ET.tostring(alert_el, encoding="unicode")
