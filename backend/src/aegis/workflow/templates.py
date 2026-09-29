"""内置防控任务工作流模板（灾种处置剧本的可视化载体）。

模板即"≥10 类节点自定义配置"的现成示例：画布可加载后拖拽改参，
修订产生新版本，在途实例仍按创建时快照执行。
"""

from __future__ import annotations

from typing import Any

from aegis.workflow.engine import WorkflowEngine

DEBRIS_FLOW_TEMPLATE: dict[str, Any] = {
    "name": "泥石流预警标准流程",
    "description": "雨强与泥位双条件触发 → 定级 → 生成预警 → 指挥员核签 → 多通道发布 → 回执采集",
    "nodes": [
        {"node_id": "fetch_rain", "type": "data_fetch", "name": "拉取区域读数", "config": {"limit": 500}},
        {
            "node_id": "check_trigger",
            "type": "threshold",
            "name": "触发条件判定",
            "config": {
                "upstream": "fetch_rain",
                "mode": "all",
                "conditions": [
                    {"metric": "rain_10min", "op": ">=", "threshold": 30.0, "agg": "max"},
                    {"metric": "debris_level", "op": ">=", "threshold": 1.0, "agg": "max"},
                ],
            },
        },
        {"node_id": "assess", "type": "risk_assess", "name": "风险定级", "config": {"upstream": "check_trigger"}},
        {"node_id": "generate", "type": "warning_generate", "name": "生成预警", "config": {}},
        {
            "node_id": "commander_sign",
            "type": "human_review",
            "name": "指挥员核签",
            "config": {"prompt": "请确认是否发布该预警", "options": ["approve", "reject"]},
            "sla_ms": 60_000,
            "timeout_ms": 60_000,
            "on_failure": "escalate",
        },
        {"node_id": "publish", "type": "warning_publish", "name": "靶向发布", "config": {"channels": ["sms", "broadcast", "wechat"]}},
        {"node_id": "feedback", "type": "feedback_collect", "name": "触达回执", "config": {}},
        {"node_id": "log_quiet", "type": "notify", "name": "未触发记录", "config": {"text": "本轮未触发泥石流预警", "level": "debug"}},
    ],
    "edges": [
        {"source": "fetch_rain", "target": "check_trigger"},
        {"source": "check_trigger", "target": "assess", "condition": "triggered"},
        {"source": "check_trigger", "target": "log_quiet", "condition": "not_triggered"},
        {"source": "assess", "target": "generate"},
        {"source": "generate", "target": "commander_sign"},
        {"source": "commander_sign", "target": "publish", "condition": "approve"},
        {"source": "publish", "target": "feedback"},
    ],
}

LAKE_OUTBURST_TEMPLATE: dict[str, Any] = {
    "name": "冰湖溃决应急会商流程",
    "description": "水位与渗浊双条件 → 灾种识别 → 定级 → 态势推演 → 高风险直接发布并通知，中低风险加密监测",
    "nodes": [
        {"node_id": "fetch_lake", "type": "data_fetch", "name": "拉取冰湖区读数", "config": {"limit": 500}},
        {
            "node_id": "check_trigger",
            "type": "threshold",
            "name": "溃决前兆判定",
            "config": {
                "upstream": "fetch_lake",
                "mode": "any",
                "conditions": [
                    {"metric": "lake_level_m", "op": ">=", "threshold": 0.5, "agg": "max"},
                    {"metric": "dam_seepage_turbidity_ntu", "op": ">=", "threshold": 50.0, "agg": "max"},
                ],
            },
        },
        {"node_id": "assess", "type": "risk_assess", "name": "风险定级", "config": {"upstream": "check_trigger"}},
        {"node_id": "simulate", "type": "situation_simulate", "name": "溃决态势推演", "config": {}},
        {"node_id": "generate", "type": "warning_generate", "name": "生成预警", "config": {}},
        {
            "node_id": "publish",
            "type": "warning_publish",
            "name": "红色预警发布（含北斗）",
            "config": {"channels": ["sms", "beidou", "broadcast"]},
        },
        {"node_id": "watch", "type": "notify", "name": "转加密监测", "config": {"text": "中低风险，转加密监测与巡查", "level": "info"}},
    ],
    "edges": [
        {"source": "fetch_lake", "target": "check_trigger"},
        {"source": "check_trigger", "target": "assess", "condition": "triggered"},
        {"source": "assess", "target": "simulate"},
        {"source": "simulate", "target": "generate"},
        {"source": "generate", "target": "publish"},
        {"source": "publish", "target": "watch"},
    ],
}

BUILTIN_TEMPLATES: tuple[dict[str, Any], ...] = (DEBRIS_FLOW_TEMPLATE, LAKE_OUTBURST_TEMPLATE)


async def register_builtin_templates(engine: WorkflowEngine, *, force: bool = False) -> list[str]:
    """幂等注册内置模板：已存在同名模板则跳过（除非 force 产生新版本）。"""
    created: list[str] = []
    for template in BUILTIN_TEMPLATES:
        existing = engine.latest_definition(template["name"])
        if existing is not None and not force:
            continue
        await engine.create_definition(
            name=template["name"],
            description=template["description"],
            nodes=template["nodes"],
            edges=template["edges"],
        )
        created.append(template["name"])
    return created
