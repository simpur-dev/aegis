"""事实叙述：认知镜像的确定性部分。

这里是"无 LLM 也能出完整答复"的一半：所有句子都由动作返回的事实拼成，措辞层（`polish`）
只能改写这些句子，不能新增数字。把叙述与动作放在同一个包里，是为了让"一个动作该怎么说"
和"它做了什么"改在一起——分开放迟早会出现动作加了字段、叙述还在读旧键。
"""

from __future__ import annotations

from typing import Any

from aegis.domain.enums import HazardType


def narrate_warning(record: dict[str, Any], chain: dict[str, Any] | None = None) -> str:
    lines = [
        f"预警 {record.get('warning_id', '')}",
        f"灾种 {record.get('hazard_type', '')}，等级 {record.get('risk_level', '')} 级，区域 {'、'.join(record.get('region_codes') or [])}",
        f"生成 {record.get('generated_at', '')}，发布 {record.get('released_at') or '未发布'}",
    ]
    deliveries = record.get("deliveries") or []
    if deliveries:
        rendered = "；".join(
            f"{item.get('channel')}={item.get('status')}" + (f"({item.get('provider_msg_id')})" if item.get("provider_msg_id") else "")
            for item in deliveries
            if isinstance(item, dict)
        )
        if rendered:
            lines.append("通道回执：" + rendered)
    if record.get("translation_pending"):
        lines.append("藏汉译文待译（LLM 译文未生成，正文以中文为准）")
    if chain:
        lines.append(narrate_chain(chain))
    return "\n".join(lines)


def narrate_chain(chain: dict[str, Any]) -> str:
    stages = chain.get("stages") or []
    parts = [f"{item.get('name')}:{item.get('mode')}" + (f"({item.get('note')})" if item.get("note") else "") for item in stages]
    lines = ["链路段落：" + " → ".join(parts)] if parts else []
    risk = chain.get("risk") or {}
    if risk:
        lines.append(f"定级依据：{str(risk.get('rationale', ''))[:300]}（{risk.get('assessed_by', '')}）")
    if chain.get("degradations"):
        lines.append("降级留痕：" + "；".join(str(item) for item in chain["degradations"]))
    if chain.get("errors"):
        lines.append("错误：" + "；".join(str(item) for item in chain["errors"]))
    if chain.get("reference_cases"):
        lines.append("参考案例：" + "、".join(str(item) for item in chain["reference_cases"]))
    return "\n".join(lines)


def narrate_plan(hazard: HazardType | None, steps: list[dict[str, Any]], cases: list[dict[str, Any]]) -> str:
    title = f"{hazard.cn if hazard else '未定灾种'}处置要点"
    lines = [title] + [
        f"{index + 1}. [{step['task_type']}] {step['objective']}（责任角色 {step['owner_role']}，时限 {step['sla_seconds']} 秒）"
        for index, step in enumerate(steps)
    ]
    if cases:
        lines.append("历史案例佐证：")
        lines.extend(_case_line(case) for case in cases[:3])
    else:
        lines.append("历史案例佐证：无命中（知识腿未装配或召不回，此处不臆造经验）")
    return "\n".join(lines)


def _case_line(case: dict[str, Any]) -> str:
    label = case.get("title") or case.get("case_id") or case.get("source") or "案例"
    summary = str(case.get("summary") or case.get("text") or "")[:160]
    return f"- {label}：{summary}"


def narrate_result(action: str, result: dict[str, Any]) -> str:
    if "error" in result:
        return str(result["error"])
    if result.get("found") is False:
        # 「没找到」必须说出口。`_brief` 会滤掉假值字段，于是 explain 落空时那句话只剩
        # `warning_id=wrn_…`——读起来像查到了，而 `found: False` 这个事实被丢了（真机量过）。
        ident = str(result.get("warning_id") or result.get("trace_id") or "").strip()
        note = str(result.get("note") or "").strip()
        reason = note or "预警与链路的读视图只保留本进程最近若干条"
        return f"没找到 {ident}：{reason}" if ident else f"没找到这条记录：{reason}"
    if action == "metric.report":
        metrics = result.get("metrics") or {}
        # 键名中性（p50/p95）、单位随 payload：这里若还按 `p50_ms` 读，秒制那几条会静默变 None
        lines = [
            f"{name}：n={stats.get('count')} p50={stats.get('p50')}{stats.get('unit')} "
            f"p95={stats.get('p95')}{stats.get('unit')} 违约={stats.get('breaches', 0)}"
            for name, stats in metrics.items()
            if isinstance(stats, dict) and "count" in stats
        ]
        return "\n".join(lines) or "尚无时延样本"
    if action == "agent.list":
        agents = result.get("agents") or {}
        items = agents.get("items") or []
        return f"在线智能体 {agents.get('online', len(items))} 个：" + "、".join(
            str(item.get("agent_id", item)) for item in items[:10] if isinstance(item, dict)
        )
    if action in {"warning.list", "chain.list", "station.list", "task.list"}:
        items = result.get("items") or []
        if not items:
            return "没有查到记录（读视图为空）"
        return f"共 {result.get('count', len(items))} 条，前 {len(items)} 条：" + "\n" + "\n".join(_brief(item) for item in items)
    if action == "task.get":
        task = result.get("task")
        return "未找到该任务单元" if task is None else _brief(task)
    if action == "drill.start":
        return "演练已完成：" + _brief(result.get("drill") or {})
    if action == "report.submit":
        return "上报已受理：" + _brief(result.get("report") or {})
    if action == "action.status":
        return _brief(result.get("action") or {})
    return _brief(result)


def _brief(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)[:200]
    keys = (
        "warning_id",
        "event_id",
        "trace_id",
        "task_unit_id",
        "station_id",
        "agent_id",
        "hazard_type",
        "risk_level",
        "region_code",
        "title_zh",
        "objective",
        "status",
        "ok",
    )
    return "；".join(f"{key}={item[key]}" for key in keys if item.get(key) not in (None, "", [], {}))[:400]


def trim_metrics(report: dict[str, Any]) -> dict[str, Any]:
    """指标答复只给分位数与违约数，不给 store 快照：状态面里的东西不必全塞进对话框。"""
    metrics = report.get("metrics") if isinstance(report, dict) else None
    trimmed: dict[str, Any] = {}
    for name, stats in (metrics or {}).items():
        if isinstance(stats, dict):
            trimmed[name] = {key: stats[key] for key in ("count", "unit", "p50", "p95", "p99", "max", "breaches") if key in stats}
    trimmed["collaboration"] = report.get("collaboration") if isinstance(report, dict) else None
    return trimmed
