"""五灾种内置模板的门禁：灾种覆盖 5/5、节点类型都在注册表里、阈值不另立真源。

为什么拿 `default_rulebook()` 来比对模板，而不是在测试里把阈值再抄一遍：
测试里重抄一遍，就等于当场造出第二处真源——规则库改了、模板没跟上时，
抄了同一串数字的门禁会跟着旧数字一起绿（架构铁律 4：同一事实只允许一处真源）。
这里只 import 规则库，把模板每条阈值条件的 (metric, op, threshold, agg) 四元组
拿去规则库里逐个指认，指不出来就是漂移。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import pytest

from aegis.config import Settings
from aegis.domain.enums import HazardType
from aegis.observability.tracer import Tracer
from aegis.services.trigger_rules import default_rulebook
from aegis.workflow.engine import WorkflowEngine
from aegis.workflow.model import NodeDef, WorkflowDef
from aegis.workflow.nodes import default_registry
from aegis.workflow.store import WorkflowRepository
from aegis.workflow.templates import BUILTIN_TEMPLATES, register_builtin_templates

# 考核指标 2 的五类高原灾种（quake_triggered 是灾害链口径、unknown 是兜底，都不该有独立剧本）
FIVE_HAZARDS: set[HazardType] = {
    HazardType.DEBRIS_FLOW,
    HazardType.LAKE_OUTBURST,
    HazardType.LANDSLIDE,
    HazardType.ROCKFALL,
    HazardType.AVALANCHE,
}

# `nodes._threshold` 只实现 max / sum / 末值三种聚合（写死在那句三元表达式里）。
# 规则库另有一条 agg="rate" 的条件（R-LANDSLIDE-2）：把它的数字抄进模板不会报错，
# 但节点会按"末值"判定——阈值一致而判定语义不一致，正是最难查的那种漂移，所以连 agg 一起核。
NODE_IMPLEMENTED_AGGS = {"max", "sum", "last"}

# RFC 2606 保留的文档域名（含其全部子域）：永远不会解析成真实内网服务，
# 因此可以安全地当占位地址写进模板，真实主机由编排者在画布上替换。
RESERVED_PLACEHOLDER_DOMAINS = ("example.com", "example.org", "example.net")


def _is_placeholder_host(host: str) -> bool:
    return host in RESERVED_PLACEHOLDER_DOMAINS or host.endswith(tuple(f".{domain}" for domain in RESERVED_PLACEHOLDER_DOMAINS))


REQUIRED_NODE_TYPES = {
    "data_fetch",
    "threshold",
    "risk_assess",
    "situation_simulate",
    "warning_generate",
    "warning_publish",
    "feedback_collect",
    "human_review",
    "notify",
}
OUTBOUND_NODE_TYPES = {"api_call", "device_control"}

ConditionKey = tuple[str, str, float, str]


def _rulebook_index() -> dict[ConditionKey, set[HazardType]]:
    """规则库条件 → 归属灾种。灾种由规则本身给出，不由测试猜。"""
    index: dict[ConditionKey, set[HazardType]] = {}
    for rule in default_rulebook():
        for condition in rule.conditions:
            key: ConditionKey = (condition.metric, condition.op, float(condition.threshold), condition.agg)
            index.setdefault(key, set()).add(rule.hazard_type)
    return index


RULEBOOK_INDEX = _rulebook_index()


def _template_conditions(template: dict[str, Any]) -> list[ConditionKey]:
    keys: list[ConditionKey] = []
    for node in template["nodes"]:
        if node["type"] != "threshold":
            continue
        for condition in node["config"]["conditions"]:
            keys.append((str(condition["metric"]), str(condition["op"]), float(condition["threshold"]), str(condition.get("agg", "last"))))
    return keys


def _hazards_of(template: dict[str, Any]) -> set[HazardType]:
    """模板归属的灾种 = 其全部阈值条件所属灾种的交集（交集为空即指认不出，可能是抄来的阈值）。"""
    keys = _template_conditions(template)
    if not keys:
        return set()
    hazards: set[HazardType] | None = None
    for key in keys:
        owned = RULEBOOK_INDEX.get(key, set())
        hazards = owned if hazards is None else hazards & owned
    return hazards or set()


def _nodes_by_id(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(node["node_id"]): node for node in template["nodes"]}


def _upstream_ids(node: dict[str, Any]) -> list[str]:
    raw = node["config"].get("upstream")
    items = raw if isinstance(raw, list) else [raw]
    return [str(item) for item in items if isinstance(item, str)]


def _template_names() -> list[str]:
    return [str(template["name"]) for template in BUILTIN_TEMPLATES]


def make_engine() -> WorkflowEngine:
    """注册模板只走定义期校验，节点服务一律不给：装配与运行是另一条门禁（分派矩阵）的事。"""
    return WorkflowEngine(repository=WorkflowRepository(), tracer=Tracer(), settings=Settings(env="test"))


class TestFiveHazardCoverage:
    def test_五灾种各有一套内置模板(self) -> None:
        assert len(BUILTIN_TEMPLATES) == 5, f"内置模板是 {len(BUILTIN_TEMPLATES)} 套，与『5/5 灾种覆盖』这句话不符"
        covered: set[HazardType] = set()
        for template in BUILTIN_TEMPLATES:
            covered |= _hazards_of(template)
        missing = sorted(h.value for h in FIVE_HAZARDS - covered)
        extra = sorted(h.value for h in covered - FIVE_HAZARDS)
        assert not missing and not extra, f"灾种覆盖不齐：缺 {missing}，多出 {extra}"

    def test_每套模板都能被指认出唯一灾种(self) -> None:
        """指认不出唯一灾种的剧本，要么跨了灾种，要么阈值不是规则库里那一条。"""
        for template in BUILTIN_TEMPLATES:
            hazards = sorted(h.value for h in _hazards_of(template))
            assert len(hazards) == 1, f"{template['name']} 的阈值条件无法指认唯一灾种：{hazards or '无（没有阈值节点）'}"

    def test_模板名不重复(self) -> None:
        """同名会挤进同一条版本链、互相吞版本（`store` 按 name→版本组织），五套剧本必须各占一条。"""
        names = _template_names()
        assert len(names) == len(set(names)), f"模板重名：{names}"

    def test_灾种与模板一一对应(self) -> None:
        by_hazard: dict[str, str] = {}
        for template in BUILTIN_TEMPLATES:
            (hazard,) = tuple(_hazards_of(template))
            by_hazard[hazard.value] = str(template["name"])
        assert set(by_hazard) == {hazard.value for hazard in FIVE_HAZARDS}


class TestThresholdsAreNotASecondSourceOfTruth:
    @pytest.mark.parametrize("template", BUILTIN_TEMPLATES, ids=_template_names())
    def test_每条阈值条件都在规则库里指得出(self, template: dict[str, Any]) -> None:
        keys = _template_conditions(template)
        assert keys, f"{template['name']} 没有 threshold 节点：触发条件只写在描述里等于没有触发条件"
        orphans = [key for key in keys if key not in RULEBOOK_INDEX]
        assert not orphans, f"{template['name']} 的阈值不在 default_rulebook() 里（模板抄出了第二处真源）：{orphans}"

    @pytest.mark.parametrize("template", BUILTIN_TEMPLATES, ids=_template_names())
    def test_模板用到的聚合方式都是节点真的实现过的(self, template: dict[str, Any]) -> None:
        unsupported = [key for key in _template_conditions(template) if key[3] not in NODE_IMPLEMENTED_AGGS]
        assert not unsupported, f"{template['name']} 用了 threshold 节点没实现的聚合（会静默按末值判定）：{unsupported}"

    def test_规则库里确实有被排除在外的聚合方式(self) -> None:
        """上一条"模板不许用 rate 聚合"是个真实取舍；规则库里若一条 rate 都没有，那条断言就在空转。"""
        assert any(condition.agg not in NODE_IMPLEMENTED_AGGS for rule in default_rulebook() for condition in rule.conditions)

    @pytest.mark.parametrize("template", BUILTIN_TEMPLATES, ids=_template_names())
    def test_多指标阈值的取数节点没被单指标过滤饿死(self, template: dict[str, Any]) -> None:
        """`data_fetch` 的 `metric` 是单值过滤：多指标阈值接在它后面时另一个指标一条读数都取不到，
        mode=all 便永远不触发——阈值抄对了却永远不会命中，是最难发现的假模板。"""
        nodes = _nodes_by_id(template)
        for node in template["nodes"]:
            if node["type"] != "threshold":
                continue
            metrics = {str(condition["metric"]) for condition in node["config"]["conditions"]}
            if len(metrics) <= 1:
                continue
            for upstream_id in _upstream_ids(node):
                source = nodes.get(upstream_id)
                if source is not None and source["type"] == "data_fetch":
                    assert "metric" not in source["config"], (
                        f"{template['name']} 的 {upstream_id} 按单指标取数，喂不出 {node['node_id']} 需要的多指标判定：{sorted(metrics)}"
                    )


class TestNodeTypesAndConfigs:
    @pytest.mark.parametrize("template", BUILTIN_TEMPLATES, ids=_template_names())
    def test_每个节点类型都已注册且配置合法(self, template: dict[str, Any]) -> None:
        """注册表是节点类型的唯一真源：写了未注册的类型，画布加载时才炸。"""
        registry = default_registry()
        for node in template["nodes"]:
            spec = registry.get(str(node["type"]))
            assert spec is not None, f"{template['name']} 用了未注册的节点类型 {node['type']}"
            validated = NodeDef.model_validate(node)
            spec.validate_config(validated.config)

    def test_模板集合驱动的节点类型明显多于十类(self) -> None:
        """指标 2 本体是"注册 16 类"，这条盯的是"剧本里真的用起来"的那几类。"""
        used = {str(node["type"]) for template in BUILTIN_TEMPLATES for node in template["nodes"]}
        assert used >= REQUIRED_NODE_TYPES, f"内置模板整体没驱动到这些节点类型：{sorted(REQUIRED_NODE_TYPES - used)}"
        assert used & OUTBOUND_NODE_TYPES, "外呼两类节点一个模板都没用到，16 类就只活在注册表里"
        assert len(used) >= 12, f"五套剧本合起来只用到 {len(used)} 类节点：{sorted(used)}"
        assert used <= set(default_registry().names())

    @pytest.mark.parametrize("template", BUILTIN_TEMPLATES, ids=_template_names())
    def test_外呼节点缺席时链路按降级继续而不是假装成功(self, template: dict[str, Any]) -> None:
        """`api_call`/`device_control` 只有配了 `AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS` 才真的发得出去。
        默认装配没配白名单，所以这两个节点的失败策略不能是 abort/retry/escalate——
        否则内置模板在没配外呼的现场一律跑不完；也不能读成"联动已完成"，degrade/skip 会留下降级原因。"""
        for node in template["nodes"]:
            if node["type"] in OUTBOUND_NODE_TYPES:
                assert node.get("on_failure") in ("degrade", "skip"), (
                    f"{template['name']}/{node['node_id']} 的外呼节点没配非致命策略：{node.get('on_failure')}"
                )

    @pytest.mark.parametrize("template", BUILTIN_TEMPLATES, ids=_template_names())
    def test_外呼目标只能是保留文档域名占位(self, template: dict[str, Any]) -> None:
        """模板里的地址是占位符：写成真主机的现场地址，等于在源码里放一条"已对接"的假证据，
        而白名单没配时它照样发不出去。真实地址由编排者按现场台账在画布上替换。"""
        for node in template["nodes"]:
            if node["type"] not in OUTBOUND_NODE_TYPES:
                continue
            url = str(node["config"].get("url") or node["config"].get("command_url") or "")
            parsed = urlsplit(url)
            assert parsed.scheme in ("http", "https"), f"{template['name']}/{node['node_id']} 外呼协议不合法：{url}"
            host = (parsed.hostname or "").lower()
            assert _is_placeholder_host(host), f"{template['name']}/{node['node_id']} 的外呼地址不是占位域名：{host!r}"


class TestTemplatesInstantiateInAssembledEngine:
    """『画布可一键实例化』这句得有人真按一次按钮：这里用装配好的容器逐套跑一遍。

    空载（现场还没读数）是这套剧本最有代表性的第一态：阈值判为未触发，
    下游按级联跳过。这里断言的不是"跑了几个节点"，而是**模板自己声明的那条
    `not_triggered` 边真的被执行了**——剧本写了"未触发要做什么"却没做到，
    比整张图安静地跳过更危险（架构铁律 7：降级必须可见）。
    """

    async def test_五套模板在装配引擎里都能跑完并兑现声明的未触发分支(self) -> None:
        from aegis.container import create_container

        container = create_container(
            Settings(
                env="test",
                bus_backend="memory",
                store_backend="memory",
                delivery_mode="mock",
                simulator_enabled=False,
                analytics_backend="off",
            ),
            with_simulator=False,
        )
        await container.start()
        try:
            for template in BUILTIN_TEMPLATES:
                name = str(template["name"])
                detail = await container.workflow.start_from_name(name, trace_id="trc_" + "b" * 16, payload={})
                assert detail["status"] == "succeeded", f"{name} 空载跑不完：{detail}"
                states = {node["state"] for node in detail["nodes"]}
                assert states <= {"succeeded", "skipped"}, f"{name} 有空载态下不该出现的节点状态：{states}"
                quiet_targets = {str(edge["target"]) for edge in template["edges"] if str(edge.get("condition", "")) == "not_triggered"}
                if not quiet_targets:
                    continue
                ran = {node["node_id"] for node in detail["nodes"] if node["state"] == "succeeded"}
                assert quiet_targets <= ran, f"{name} 声明了未触发分支却没执行：{sorted(quiet_targets - ran)}"
        finally:
            await container.shutdown()

    def test_至少四套模板写了未触发时的动作(self) -> None:
        """数量下限而不是逐套点名：五套全配是理想态，但门禁要挡的是"剧本集体不写未触发"。
        当前唯一没有未触发落点的是冰湖溃决那套（它的 notify 挂在低等级分支上）。"""
        with_quiet_branch = [
            template
            for template in BUILTIN_TEMPLATES
            if any(str(edge.get("condition", "")) == "not_triggered" for edge in template["edges"])
        ]
        assert len(with_quiet_branch) >= 4, f"只有 {len(with_quiet_branch)} 套剧本写了未触发动作"


class TestRegistrationIsIdempotent:
    async def test_首次注册五套全部落库(self) -> None:
        engine = make_engine()
        created = await register_builtin_templates(engine)
        assert created == _template_names()
        for name in _template_names():
            definition = engine.latest_definition(name)
            assert isinstance(definition, WorkflowDef), f"{name} 没建起来"
            assert definition.version == 1
            assert len(definition.nodes) >= 3, f"{name} 的节点数少得不像一套剧本"

    async def test_重复注册不产生新版本也不重复落库(self) -> None:
        """`container.start()` 每次进程都要调它：不幂等就是每次重启往版本链上堆一层。"""
        engine = make_engine()
        await register_builtin_templates(engine)
        again = await register_builtin_templates(engine)
        assert again == []
        assert len(engine.repository) == len(BUILTIN_TEMPLATES)
        for name in _template_names():
            assert engine.repository.versions(name) == [1]

    async def test_force_为每套模板各产生一个新版本(self) -> None:
        """`force` 的语义是"内置模板改了要重注册"，而定义不可变：只能产生新版本，
        在途实例仍绑定旧版本快照（这是引擎"柔性"的第一处体现，不能被 force 绕开）。"""
        engine = make_engine()
        await register_builtin_templates(engine)
        forced = await register_builtin_templates(engine, force=True)
        assert forced == _template_names()
        for name in _template_names():
            assert engine.repository.versions(name) == [1, 2]
            definition = engine.latest_definition(name)
            assert definition is not None and definition.version == 2
