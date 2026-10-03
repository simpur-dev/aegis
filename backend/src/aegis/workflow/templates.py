"""内置防控任务工作流模板（灾种处置剧本的可视化载体）。

模板即"≥10 类节点自定义配置"的现成示例：画布可加载后拖拽改参，
修订产生新版本，在途实例仍按创建时快照执行。

五灾种各一套（考核指标 2 的补强项）不是五张随手画的图，两条硬口径：

1. **阈值不另立真源**。每个 `threshold` 节点的 (metric, op, threshold, agg) 四元组都必须与
   `services/trigger_rules.py` 的 `default_rulebook()` 里的某条条件逐字一致——阈值一旦在模板里
   抄成第二份，现场改规则库时画布上的剧本就会静默按旧阈值判定（架构铁律 4，有门禁守着）。
2. **外呼两类节点的目标是占位符**。`api_call` / `device_control` 的地址与设备号属于现场台账，
   由编排者在画布上替换；这里写的是 RFC 2606 保留文档域 `api.example.com`（永远不会解析成
   真实内网服务）。而且这两个节点的 `on_failure` 一律是 `degrade`：没配
   `AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS` 时链路按降级继续并留痕，而不是把"指令没送到"读成"联动已完成"。
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

LANDSLIDE_TEMPLATE: dict[str, Any] = {
    "name": "滑坡隐患点降雨-位移双判处置流程",
    "description": "降雨入渗叠加位移 → 灾种识别 → 定级 → 案例态势推演 → 指挥员核签 → 生成并发布 → 回执；未触发转人工加密巡查",
    "nodes": [
        {"node_id": "fetch_watch", "type": "data_fetch", "name": "拉取隐患点读数", "config": {"limit": 500}},
        {
            "node_id": "check_infiltration",
            "type": "threshold",
            "name": "降雨入渗叠加位移判定",
            "config": {
                "upstream": "fetch_watch",
                "mode": "all",
                "conditions": [
                    {"metric": "rain_cumulative_24h", "op": ">=", "threshold": 60.0, "agg": "max"},
                    {"metric": "displacement_mm", "op": ">=", "threshold": 20.0, "agg": "max"},
                ],
            },
        },
        # 滑坡与泥石流在共沟沟谷里常同时抬头，识别节点把候选灾种摊开，定级才不会被单一阈值绑架。
        {"node_id": "identify_hazard", "type": "hazard_identify", "name": "灾种识别", "config": {}},
        {"node_id": "assess", "type": "risk_assess", "name": "风险定级", "config": {"upstream": ["check_infiltration", "identify_hazard"]}},
        {"node_id": "simulate", "type": "situation_simulate", "name": "滑动态势推演", "config": {"extra": {"horizon_minutes": 120}}},
        {
            "node_id": "commander_sign",
            "type": "human_review",
            "name": "指挥员核签转移与预警",
            "config": {"prompt": "是否组织受威胁群众转移并签发滑坡预警", "options": ["approve", "reject"]},
            "sla_ms": 60_000,
            "timeout_ms": 60_000,
            "on_failure": "escalate",
        },
        {"node_id": "generate", "type": "warning_generate", "name": "生成预警", "config": {}},
        {"node_id": "publish", "type": "warning_publish", "name": "靶向发布", "config": {"channels": ["sms", "broadcast", "wechat"]}},
        {"node_id": "feedback", "type": "feedback_collect", "name": "触达回执", "config": {}},
        {
            "node_id": "patrol",
            "type": "notify",
            "name": "转人工巡查",
            "config": {"text": "本轮未触发滑坡阈值，转人工加密巡查", "level": "info"},
        },
    ],
    "edges": [
        {"source": "fetch_watch", "target": "check_infiltration"},
        {"source": "check_infiltration", "target": "identify_hazard", "condition": "triggered"},
        {"source": "check_infiltration", "target": "assess", "condition": "triggered"},
        {"source": "identify_hazard", "target": "assess"},
        {"source": "assess", "target": "simulate"},
        {"source": "simulate", "target": "commander_sign"},
        {"source": "commander_sign", "target": "generate", "condition": "approve"},
        {"source": "publish", "target": "feedback"},
        {"source": "generate", "target": "publish"},
        {"source": "check_infiltration", "target": "patrol", "condition": "not_triggered"},
    ],
}

ROCKFALL_TEMPLATE: dict[str, Any] = {
    "name": "崩塌危岩冻融期管控流程",
    "description": "冻融循环叠加裂缝扩展 → 定级 → 拦路闸联动与预警发布两路并行 → 汇聚后采回执；联动缺席时按降级继续，不谎报已封路",
    "nodes": [
        # 不给 data_fetch 设 metric 过滤：本剧本的阈值要吃两类指标，按单指标过滤会让另一类永远取不到值。
        {"node_id": "fetch_rock", "type": "data_fetch", "name": "拉取危岩区读数", "config": {"limit": 300}},
        {
            "node_id": "check_freeze_thaw",
            "type": "threshold",
            "name": "冻融裂扩展判定",
            "config": {
                "upstream": "fetch_rock",
                "mode": "all",
                "conditions": [
                    {"metric": "freeze_thaw_cycles", "op": ">=", "threshold": 3.0, "agg": "max"},
                    {"metric": "crack_aperture_mm", "op": ">=", "threshold": 15.0, "agg": "max"},
                ],
            },
        },
        {"node_id": "assess", "type": "risk_assess", "name": "风险定级", "config": {"upstream": "check_freeze_thaw"}},
        {
            "node_id": "barrier",
            "type": "device_control",
            "name": "危岩段拦路闸联动（占位地址，需现场台账替换）",
            "config": {"device": "road-barrier-01", "action": "close", "command_url": "https://api.example.com/device/command"},
            "on_failure": "degrade",
        },
        {"node_id": "generate", "type": "warning_generate", "name": "生成预警", "config": {}},
        {
            "node_id": "publish",
            "type": "warning_publish",
            "name": "靶向发布（含北斗）",
            "config": {"channels": ["sms", "beidou", "broadcast"]},
        },
        {"node_id": "sync", "type": "join", "name": "联动与触达汇聚", "config": {"upstream": ["barrier", "publish"]}},
        {"node_id": "feedback", "type": "feedback_collect", "name": "触达回执", "config": {}},
        {"node_id": "log_quiet", "type": "notify", "name": "未触发记录", "config": {"text": "本轮未触发崩塌阈值", "level": "debug"}},
    ],
    "edges": [
        {"source": "fetch_rock", "target": "check_freeze_thaw"},
        {"source": "check_freeze_thaw", "target": "assess", "condition": "triggered"},
        {"source": "check_freeze_thaw", "target": "log_quiet", "condition": "not_triggered"},
        {"source": "assess", "target": "barrier"},
        {"source": "assess", "target": "generate"},
        {"source": "generate", "target": "publish"},
        {"source": "barrier", "target": "sync"},
        {"source": "publish", "target": "sync"},
        {"source": "sync", "target": "feedback"},
    ],
}

AVALANCHE_TEMPLATE: dict[str, Any] = {
    "name": "雪崩气象型预警与交通管控流程",
    "description": "新雪叠加强风走定级发布链，气温骤升叠加雪水当量走弱层加密观测支路；高风险先调取公路雪况实况（占位地址，需白名单）",
    "nodes": [
        {"node_id": "fetch_snow", "type": "data_fetch", "name": "拉取积雪气象读数", "config": {"limit": 400}},
        {
            "node_id": "check_snow_load",
            "type": "threshold",
            "name": "新雪叠加强风吹雪判定",
            "config": {
                "upstream": "fetch_snow",
                "mode": "all",
                "conditions": [
                    {"metric": "new_snow_cm", "op": ">=", "threshold": 25.0, "agg": "max"},
                    {"metric": "wind_speed_ms", "op": ">=", "threshold": 14.0, "agg": "max"},
                ],
            },
        },
        {
            "node_id": "check_weak_layer",
            "type": "threshold",
            "name": "气温骤升弱层失稳判定",
            "config": {
                "upstream": "fetch_snow",
                "mode": "all",
                "conditions": [
                    {"metric": "air_temperature_c", "op": ">=", "threshold": 2.0, "agg": "max"},
                    {"metric": "snow_water_equivalent_mm", "op": ">=", "threshold": 40.0, "agg": "max"},
                ],
            },
        },
        {"node_id": "assess", "type": "risk_assess", "name": "风险定级", "config": {"upstream": "check_snow_load"}},
        {
            "node_id": "route",
            "type": "branch",
            "name": "按等级决定是否先调取雪况实况",
            "config": {
                "upstream": "assess",
                "rules": [{"when": "risk_level", "op": "<=", "value": 2, "then": "traffic_check"}],
                "default": "publish_direct",
            },
        },
        {
            "node_id": "traffic_intel",
            "type": "api_call",
            "name": "调取公路雪况实况（占位地址，需现场替换）",
            "config": {"method": "GET", "url": "https://api.example.com/snow-intel"},
            "on_failure": "degrade",
        },
        {"node_id": "generate", "type": "warning_generate", "name": "生成预警", "config": {}},
        {"node_id": "publish", "type": "warning_publish", "name": "靶向发布", "config": {"channels": ["sms", "broadcast"]}},
        {"node_id": "feedback", "type": "feedback_collect", "name": "触达回执", "config": {}},
        {
            "node_id": "pit_watch",
            "type": "notify",
            "name": "弱层失稳转雪坑观测",
            "config": {"text": "气温回升叠加强融雪，雪层弱层失稳，转雪坑与人工加密观测", "level": "warning"},
        },
        {"node_id": "log_quiet", "type": "notify", "name": "未触发记录", "config": {"text": "本轮未触发雪崩荷载阈值", "level": "debug"}},
    ],
    "edges": [
        {"source": "fetch_snow", "target": "check_snow_load"},
        {"source": "fetch_snow", "target": "check_weak_layer"},
        {"source": "check_snow_load", "target": "assess", "condition": "triggered"},
        {"source": "check_snow_load", "target": "log_quiet", "condition": "not_triggered"},
        {"source": "check_weak_layer", "target": "pit_watch", "condition": "triggered"},
        {"source": "assess", "target": "route"},
        {"source": "route", "target": "traffic_intel", "condition": "traffic_check"},
        {"source": "route", "target": "generate", "condition": "publish_direct"},
        {"source": "traffic_intel", "target": "generate"},
        {"source": "generate", "target": "publish"},
        {"source": "publish", "target": "feedback"},
    ],
}

BUILTIN_TEMPLATES: tuple[dict[str, Any], ...] = (
    DEBRIS_FLOW_TEMPLATE,
    LAKE_OUTBURST_TEMPLATE,
    LANDSLIDE_TEMPLATE,
    ROCKFALL_TEMPLATE,
    AVALANCHE_TEMPLATE,
)


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
