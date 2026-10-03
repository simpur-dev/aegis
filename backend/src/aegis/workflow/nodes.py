"""工作流节点类型注册表（考核要求"≥10 类防控任务节点自定义配置"的落点）。

新增节点 = 注册一个 NodeSpec（声明必填配置 + 异步 handler），不改引擎内核：
这是"柔性"的第二处体现，也是画布上节点面板的数据来源。

handler 不直接依赖具体服务：全部通过 WorkflowServices 注入，
因此节点逻辑可离线单测，智能体/真实通道上线时只需替换注入实现。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aegis.errors import AegisError, ErrorCode


class NodeError(AegisError):
    """节点执行失败（可重试由 retryable 决定）。"""

    code = ErrorCode.INTERNAL


class NodeConfigError(AegisError):
    """节点配置不合法：属于定义期错误，不重试、直接判失败。"""

    code = ErrorCode.SCHEMA_INVALID


class HumanRequired(Exception):
    """节点等待人工决策（会签/审批），引擎挂起实例。"""

    def __init__(self, prompt: str = "", options: tuple[str, ...] = ("approve", "reject")) -> None:
        super().__init__(prompt)
        self.prompt = prompt
        self.options = options


class NodeOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output: dict[str, Any] = Field(default_factory=dict)
    branch: str | None = None  # 分支节点产出的分支名，用于边条件匹配
    note: str = Field(default="", max_length=256)


@dataclass(slots=True)
class WorkflowServices:
    """节点可依赖的外部能力（全部可注入，便于测试与替换实现）。"""

    telemetry_query: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None
    identify: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    assess: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    simulate: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    generate_warning: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    publish_warning: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    collect_feedback: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    notify: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    http_call: Callable[..., Awaitable[dict[str, Any]]] | None = None


@dataclass(frozen=True, slots=True)
class NodeContext:
    payload: dict[str, Any]
    inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    all_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    services: WorkflowServices = field(default_factory=WorkflowServices)
    attempt: int = 1
    decision: dict[str, Any] | None = None

    def upstream(self, node_id: str) -> dict[str, Any]:
        return self.inputs.get(node_id, {})

    def find_output(self, key: str) -> Any:
        """在全部已完成节点的输出里找某个键：链路过长时下游仍可取到上游结论。"""
        for source in (self.inputs, self.all_outputs):
            for output in source.values():
                if isinstance(output, dict) and key in output:
                    return output[key]
        return None

    def require_service(self, attr: str, node_type: str) -> Callable[..., Awaitable[Any]]:
        service = getattr(self.services, attr, None)
        if service is None:
            raise NodeError(f"节点 {node_type} 缺少依赖服务 {attr}", detail={"node_type": node_type, "service": attr})
        return service


NodeHandler = Callable[[NodeContext, dict[str, Any]], Awaitable[NodeOutcome]]


@dataclass(frozen=True, slots=True)
class NodeSpec:
    type_name: str
    description: str
    handler: NodeHandler
    required_config: tuple[str, ...] = ()
    optional_config: tuple[str, ...] = ()
    min_config: dict[str, Any] = field(default_factory=dict)

    def validate_config(self, config: dict[str, Any]) -> dict[str, Any]:
        missing = [key for key in self.required_config if key not in config or config[key] in (None, "")]
        if missing:
            raise NodeConfigError(f"节点 {self.type_name} 缺少必填参数: {missing}", detail={"missing": missing})
        unknown = [key for key in config if key not in self.required_config and key not in self.optional_config]
        if unknown:
            raise NodeConfigError(
                f"节点 {self.type_name} 含未知参数: {unknown}",
                detail={"unknown": unknown, "allowed": [*self.required_config, *self.optional_config]},
            )
        for key, floor in self.min_config.items():
            if key in config and isinstance(config[key], (int, float)) and config[key] < floor:
                raise NodeConfigError(f"节点 {self.type_name} 参数 {key} 不得小于 {floor}")
        return config


class NodeRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, NodeSpec] = {}

    def register(self, spec: NodeSpec) -> None:
        if spec.type_name in self._specs:
            raise ValueError(f"节点类型重复注册: {spec.type_name}")
        self._specs[spec.type_name] = spec

    def get(self, type_name: str) -> NodeSpec | None:
        return self._specs.get(type_name)

    def require(self, type_name: str) -> NodeSpec:
        spec = self._specs.get(type_name)
        if spec is None:
            raise NodeConfigError(f"未注册的节点类型: {type_name}", detail={"known": self.names()})
        return spec

    def names(self) -> list[str]:
        return sorted(self._specs)

    def __len__(self) -> int:
        return len(self._specs)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _compare(actual: Any, op: str, expected: Any) -> bool:
    ops = {
        ">=": lambda a, b: a >= b,
        ">": lambda a, b: a > b,
        "<=": lambda a, b: a <= b,
        "<": lambda a, b: a < b,
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
        "in": lambda a, b: a in _as_list(b),
        "not_in": lambda a, b: a not in _as_list(b),
    }
    if op not in ops:
        raise NodeConfigError(f"不支持的比较算子: {op}", detail={"supported": sorted(ops)})
    try:
        return bool(ops[op](actual, expected))
    except TypeError as exc:
        raise NodeError(f"阈值比较失败: {actual!r} {op} {expected!r}", detail={"reason": str(exc)}) from exc


def _dig(source: dict[str, Any], path: str) -> Any:
    current: Any = source
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


async def _data_fetch(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    query = ctx.require_service("telemetry_query", "data_fetch")
    rows = await query(
        region_code=config.get("region_code"),
        metric=config.get("metric"),
        limit=int(config.get("limit", 200)),
    )
    return NodeOutcome(output={"rows": rows, "count": len(rows)}, note=f"取回 {len(rows)} 条读数")


async def _threshold(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    """对上游 data_fetch 的行做阈值判定；任一条件命中即 triggered（mode=all 时全满足）。"""
    rows = ctx.inputs.get(config["upstream"], {}).get("rows", []) if config.get("upstream") else ctx.payload.get("rows", [])
    conditions = config["conditions"]
    mode = str(config.get("mode", "any")).lower()
    outcomes: list[bool] = []
    hits: list[dict[str, Any]] = []
    for condition in conditions:
        metric = condition["metric"]
        values = [row["value"] for row in rows if row.get("metric") == metric and row.get("quality_flag", "ok") == "ok"]
        if not values:
            outcomes.append(False)
            continue
        picked = max(values) if condition.get("agg", "max") == "max" else (sum(values) if condition["agg"] == "sum" else values[-1])
        if _compare(picked, condition["op"], condition["threshold"]):
            outcomes.append(True)
            hits.append({"metric": metric, "value": picked, "rule": f"{metric}{condition['op']}{condition['threshold']}"})
        else:
            outcomes.append(False)
    triggered = all(outcomes) if mode == "all" else any(outcomes)
    return NodeOutcome(
        output={"triggered": triggered, "hits": hits},
        branch="triggered" if triggered else "not_triggered",
        note=f"命中 {sum(outcomes)}/{len(outcomes)} 条件",
    )


async def _hazard_identify(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    service = ctx.require_service("identify", "hazard_identify")
    result = await service({**ctx.payload, **config.get("extra", {}), "upstream": ctx.inputs})
    return NodeOutcome(output={"hazard": result}, branch=None)


async def _risk_assess(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    service = ctx.require_service("assess", "risk_assess")
    merged = {**ctx.payload, **config.get("extra", {})}
    for upstream_id in _as_list(config.get("upstream", [])):
        merged.update(ctx.upstream(upstream_id))
    # `hazard_identify` 把结果整体挂在 "hazard" 键下：不接这一刀，识别→定级这条边上的
    # hits 永远是空的，定级就会一路"保守四级"——画布上看着连了线，实际什么都没传过去。
    hazard = merged.get("hazard")
    if isinstance(hazard, dict):
        merged.setdefault("hits", hazard.get("hits") or [])
        hazards = hazard.get("hazards") or []
        if not merged.get("hazard_type") and len(hazards) == 1:
            merged["hazard_type"] = hazards[0]
    result = await service(merged)
    try:
        level = int(result.get("risk_level", 5))
    except (TypeError, ValueError) as exc:
        raise NodeError(f"定级结果 risk_level 非法: {result.get('risk_level')!r}") from exc
    if not 1 <= level <= 5:
        raise NodeError(f"定级结果超出 1-5 范围: {level}")
    return NodeOutcome(output={"risk_level": level, "risk": result}, branch=f"level_{level}")


def _collect_simulate_anchors(merged: dict[str, Any], sources: list[Any]) -> None:
    """从上游产出里补齐推演需要的锚点（等级、灾种、区域、命中证据）。

    上游结论的形状不止一种：`risk_assess` 把定级结论挂在 ``risk`` 子键下、`hazard_identify`
    挂在 ``hazard`` 下。只扫顶层就等于让这两条边永远拿不到灾种，案例检索只能空跑。
    """
    for source in sources:
        if not isinstance(source, dict):
            continue
        candidates: list[dict[str, Any]] = [source]
        for key in ("risk", "hazard"):
            nested = source.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
        for candidate in candidates:
            for key in ("risk_level", "hazard_type", "region_code"):
                if merged.get(key) in (None, "") and candidate.get(key) is not None:
                    merged[key] = candidate[key]
            hits = candidate.get("hits")
            if isinstance(hits, list) and hits:
                merged.setdefault("hits", hits)
                for hit in hits:
                    if isinstance(hit, dict) and hit.get("hazard_type") and not merged.get("hazard_type"):
                        merged["hazard_type"] = hit["hazard_type"]
                        break


async def _situation_simulate(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    service = ctx.require_service("simulate", "situation_simulate")
    merged: dict[str, Any] = {**ctx.payload, **config.get("extra", {}), "upstream": ctx.inputs}
    # 不往上游找，链路上的推演就只能看画布手填的 extra——"案例驱动"会变成空话。
    _collect_simulate_anchors(merged, [*ctx.inputs.values(), *ctx.all_outputs.values()])
    result = await service(merged)
    # 整份结果都要带出去：`degraded`/`trend` 被丢掉过一次，代价是画布上看不见"这版推演没有案例支撑"
    return NodeOutcome(
        output={
            "scenarios": result.get("scenarios", []),
            "horizon_minutes": result.get("horizon_minutes", 60),
            "trend": result.get("trend"),
            "degraded": bool(result.get("degraded", False)),
            "degraded_reason": result.get("degraded_reason", ""),
            "case_count": result.get("case_count", len(result.get("scenarios", []))),
            "dropped_cases": result.get("dropped_cases", 0),
            # 推演是"从哪个等级往外推"的：不带上声明等级与灾种，画布上就没法核对结论的锚点
            "declared_level": result.get("declared_level"),
            "hazard_type": result.get("hazard_type"),
            "region_code": result.get("region_code"),
        }
    )


async def _human_review(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    """会签/审批节点：首次执行挂起等待决策，恢复执行时读取 decision。"""
    decision = ctx.decision or ctx.payload.get("decision")
    if not decision:
        raise HumanRequired(str(config.get("prompt", "请值班指挥员确认")), tuple(config.get("options", ("approve", "reject"))))
    choice = str(decision.get("choice", "")).lower()
    allowed = tuple(config.get("options", ("approve", "reject")))
    if choice not in allowed:
        raise NodeError(f"人工决策值非法: {choice!r}，允许 {allowed}")
    return NodeOutcome(output={"decision": decision, "choice": choice}, branch=choice)


async def _warning_generate(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    service = ctx.require_service("generate_warning", "warning_generate")
    merged: dict[str, Any] = {**ctx.payload, **config.get("extra", {}), "upstream": ctx.inputs}
    if "risk" not in merged:
        risk = ctx.find_output("risk") or ctx.find_output("risk_level")
        if isinstance(risk, dict) and "risk_level" in risk:
            merged["risk"] = risk
        elif isinstance(risk, int):
            for output in {**ctx.inputs, **ctx.all_outputs}.values():
                if isinstance(output, dict) and "risk_level" in output:
                    merged["risk"] = output
                    break
    if "hits" not in merged:
        merged["hits"] = ctx.find_output("hits") or []
    result = await service(merged)
    return NodeOutcome(output={"warning": result})


async def _warning_publish(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    service = ctx.require_service("publish_warning", "warning_publish")
    warning = ctx.payload.get("warning") or ctx.find_output("warning")
    if not isinstance(warning, dict):
        raise NodeError("warning_publish 缺少预警对象（上游未产出 warning）")
    result = await service({"warning": warning, "channels": _as_list(config.get("channels", []))})
    delivered = int(result.get("delivered", 0))
    return NodeOutcome(
        output={"delivery": result, "delivered": delivered},
        branch="delivered" if delivered > 0 else "undelivered",
    )


async def _feedback_collect(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    service = ctx.require_service("collect_feedback", "feedback_collect")
    result = await service({**ctx.payload, "wait_seconds": config.get("wait_seconds", 0)})
    return NodeOutcome(output={"feedback": result})


async def _branch(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    """条件分支：按 rules 顺序匹配，命中即输出分支名；全部不中走 default。"""
    source = {**ctx.payload, **ctx.inputs.get(config.get("upstream", ""), {})}
    default = str(config.get("default", "else"))
    for rule in config["rules"]:
        left = _dig(source, rule["when"])
        if _compare(left, rule.get("op", "=="), rule["value"]):
            return NodeOutcome(output={"matched": rule["when"]}, branch=str(rule["then"]))
    return NodeOutcome(output={"matched": "default"}, branch=default)


async def _join(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    """并行汇聚：等待全部上游完成后合并结果（引擎已保证上游终态才调度本节点）。"""
    expected = _as_list(config.get("upstream", []))
    missing = [node_id for node_id in expected if node_id not in ctx.inputs]
    if missing:
        raise NodeError(f"汇聚节点缺少上游结果: {missing}", detail={"missing": missing})
    merged: dict[str, Any] = {}
    for node_id in expected:
        merged[node_id] = ctx.upstream(node_id)
    return NodeOutcome(output={"merged": merged})


async def _delay(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    seconds = float(config.get("seconds", 0))
    await asyncio.sleep(min(seconds, 30.0))
    return NodeOutcome(output={"delayed_seconds": min(seconds, 30.0)})


async def _notify(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    service = ctx.require_service("notify", "notify")
    await service({"level": config.get("level", "info"), "text": config.get("text", ""), "payload": ctx.payload})
    return NodeOutcome(output={"notified": True})


async def _api_call(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    service = ctx.require_service("http_call", "api_call")
    result = await service(config["method"], config["url"], config.get("body"))
    return NodeOutcome(output={"response": result})


async def _device_control(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    """设备控制（应急广播/路障/闸门等）：走 http_call 或 notify，保持可注入。"""
    service = ctx.require_service("http_call", "device_control")
    result = await service("POST", config["command_url"], {"device": config["device"], "action": config["action"]})
    return NodeOutcome(output={"device": config["device"], "response": result})


async def _degrade_to_rule(ctx: NodeContext, config: dict[str, Any]) -> NodeOutcome:
    """降级节点：智能体不可用时按规则给出保守结论，保证链路继续。"""
    fallback_level = int(config.get("risk_level", 4))
    if not 1 <= fallback_level <= 5:
        raise NodeConfigError(f"降级风险等级超出 1-5: {fallback_level}")
    return NodeOutcome(output={"risk_level": fallback_level, "degraded": True}, branch=f"level_{fallback_level}")


def default_registry() -> NodeRegistry:
    registry = NodeRegistry()
    specs = [
        NodeSpec("data_fetch", "数据接入：按区域/指标拉取最新读数", _data_fetch, (), ("region_code", "metric", "limit")),
        NodeSpec("threshold", "阈值判断：对读数做触发条件判定", _threshold, ("conditions",), ("upstream", "mode")),
        NodeSpec("hazard_identify", "灾种识别：判定候选灾种", _hazard_identify, (), ("extra",)),
        NodeSpec("risk_assess", "风险定级：产出 1-5 级结论", _risk_assess, (), ("upstream", "extra")),
        NodeSpec("situation_simulate", "态势推演：多情景趋势", _situation_simulate, (), ("extra", "upstream")),
        NodeSpec("human_review", "人工审核/会签：等待决策", _human_review, (), ("prompt", "options")),
        NodeSpec("warning_generate", "预警生成：产出分级预警内容", _warning_generate, (), ("extra",)),
        NodeSpec("warning_publish", "预警发布：多通道靶向触达", _warning_publish, (), ("channels",)),
        NodeSpec("feedback_collect", "反馈采集：回执与响应状态", _feedback_collect, (), ("wait_seconds",)),
        NodeSpec("branch", "条件分支：按规则路由", _branch, ("rules",), ("upstream", "default")),
        NodeSpec("join", "并行汇聚：合并多路上游结果", _join, ("upstream",)),
        NodeSpec("delay", "延时等待：节流与错峰", _delay, (), ("seconds",)),
        NodeSpec("notify", "通知：总线事件广播", _notify, ("text",), ("level",)),
        NodeSpec("api_call", "外部 API 调用", _api_call, ("method", "url"), ("body",)),
        NodeSpec("device_control", "设备控制：广播/路障等联动", _device_control, ("device", "action", "command_url")),
        NodeSpec("degrade_to_rule", "规则降级：智能体不可用时保守定级", _degrade_to_rule, (), ("risk_level",), {"risk_level": 1}),
    ]
    for spec in specs:
        registry.register(spec)
    return registry


__all__ = [
    "HumanRequired",
    "NodeConfigError",
    "NodeContext",
    "NodeError",
    "NodeOutcome",
    "NodeRegistry",
    "NodeSpec",
    "WorkflowServices",
    "default_registry",
]
