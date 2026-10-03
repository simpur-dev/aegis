"""`situation_simulate` 的案例驱动改造：有案例就给出处与量化要素，没案例就如实降级。

升级前的实现是 ±1 启发式（保守/中性/不利 = 等级 ±1），计划文档直接点名它是"假推演"：
输出看着像多情景，实际没有任何证据来源。现在这条腿走 `ScenarioProvider` 协议注入
（内核不得 import 知识/检索腿，架构铁律 3 有 AST 门禁），因此三件事必须被用例钉住：

1. 有案例时：情景带 `refs`（case_id）与案例真给的量化要素，数值原样搬运、不被改写；
2. 案例没给的数字一律不出现——"没测到的如实写未测得，不编数"；
3. 能力缺位/无命中/报错时：`degraded: True` 且写明原因，且结论里没有任何案例出处。

另外，画布与既有用例在读的三个键（`horizon_minutes` / `scenarios` / `trend`）不能变。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from aegis.workflow.nodes import NodeContext, WorkflowServices, default_registry
from aegis.workflow.services_bridge import DEFAULT_SCENARIO_CASE_LIMIT, ScenarioProvider, build_workflow_services

CASE_WITH_NUMBERS: dict[str, Any] = {
    "case_id": "case_avalanche_load_01",
    "title": "新雪期吹雪与交通管控预案",
    "estimated_delay_hours": 6.5,
    "applies_to_levels": [2, 3],
    "monitoring_metrics": ["new_snow_cm", "wind_speed_ms"],
    "confidence": 0.6,
    "matched_on": ["avalanche", "5401"],
    "source": "in_memory",
}
CASE_BARE: dict[str, Any] = {"case_id": "case_bare_title_only_01", "title": "只带标题与出处的案例"}
CASE_NO_ID: dict[str, Any] = {"title": "没有 case_id 的命中", "estimated_delay_hours": 12.0, "applies_to_levels": [1]}

BRIDGE_ARGS: dict[str, Any] = {
    "store": SimpleNamespace(),
    "rule_engine": SimpleNamespace(),
    "risk_engine": SimpleNamespace(),
    "warning_service": SimpleNamespace(),
    "dispatcher": SimpleNamespace(),
    "gateway": SimpleNamespace(),
}


class StubProvider:
    """协议的最简实现：记录调用参数，返回给定案例列表（不碰任何真实腿）。"""

    def __init__(self, cases: list[dict[str, Any]]) -> None:
        self.cases = cases
        self.calls: list[dict[str, Any]] = []

    async def recall_scenarios(
        self,
        *,
        hazard_type: str | None = None,
        region_code: str | None = None,
        risk_level: int | None = None,
        limit: int = DEFAULT_SCENARIO_CASE_LIMIT,
    ) -> list[dict[str, Any]]:
        self.calls.append({"hazard_type": hazard_type, "region_code": region_code, "risk_level": risk_level, "limit": limit})
        return self.cases[:limit]


class BoomProvider:
    """腿故障的形状：Neo4j 断开/检索超时在生产是真会发生的，推演不能因此把整条链路拖死。"""

    async def recall_scenarios(self, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("graphiti unavailable")


async def simulate(services: WorkflowServices, payload: dict[str, Any]) -> dict[str, Any]:
    assert services.simulate is not None
    return await services.simulate(payload)


def bridge(**overrides: Any) -> WorkflowServices:
    return build_workflow_services(**{**BRIDGE_ARGS, **overrides})


def all_refs_and_provenance(result: dict[str, Any]) -> list[Any]:
    refs: list[Any] = []
    for scenario in result["scenarios"]:
        refs.extend(scenario.get("refs") or [])
        for factor in scenario.get("impact_factors") or []:
            refs.append(factor.get("provenance"))
    return refs


class TestProviderDrivenScenarios:
    async def test_有案例时每个情景都带出处与案例真给的量化要素(self) -> None:
        services = bridge(scenario_provider=StubProvider([CASE_WITH_NUMBERS]))
        result = await simulate(services, {"risk_level": 3, "hazard_type": "avalanche", "region_code": "540121"})

        assert result["degraded"] is False
        assert result["case_count"] == 1
        scenario = result["scenarios"][0]
        assert scenario["name"] == CASE_WITH_NUMBERS["title"]
        assert scenario["refs"] == [CASE_WITH_NUMBERS["case_id"]], "情景必须能回溯到案例 id，否则出处这句话是空的"
        factors = {str(item["key"]): item for item in scenario["impact_factors"]}
        # 数值原样搬运：推演不许把案例的 6.5 小时改成"看起来更合理"的数
        assert factors["estimated_delay_hours"]["value"] == 6.5
        assert factors["estimated_delay_hours"]["provenance"] == CASE_WITH_NUMBERS["case_id"]
        assert factors["applies_to_levels"]["value"] == [2, 3]
        assert factors["monitoring_metrics"]["value"] == ["new_snow_cm", "wind_speed_ms"]
        assert factors["confidence"]["value"] == 0.6

    async def test_召回参数是灾种区域与本次定级(self) -> None:
        provider = StubProvider([CASE_WITH_NUMBERS])
        services = bridge(scenario_provider=provider)
        await simulate(services, {"risk_level": 2, "hazard_type": "landslide", "region_code": "540121", "case_limit": 2})

        assert provider.calls == [{"hazard_type": "landslide", "region_code": "540121", "risk_level": 2, "limit": 2}]

    async def test_情景等级来自案例声明而不是代码猜测(self) -> None:
        """±1 启发式的味道就是"偏移量由代码决定"。现在偏移只可能来自案例声明的适用等级：
        本次定级 4，案例适用 [2,3] → 取最近的 3；这条断言把"取哪一档"钉死。"""
        services = bridge(scenario_provider=StubProvider([CASE_WITH_NUMBERS]))
        result = await simulate(services, {"risk_level": 4, "hazard_type": "avalanche"})

        scenario = result["scenarios"][0]
        assert scenario["expected_level"] == 3
        assert scenario["expected_level"] in CASE_WITH_NUMBERS["applies_to_levels"]
        assert result["trend"] == "升级"

    async def test_案例没声明适用等级时沿用本次定级(self) -> None:
        services = bridge(scenario_provider=StubProvider([CASE_BARE]))
        result = await simulate(services, {"risk_level": 3})

        scenario = result["scenarios"][0]
        assert scenario["expected_level"] == 3
        assert "未声明适用等级" in scenario["basis"]

    async def test_案例没给的数字绝不凭空出现(self) -> None:
        """只带标题的案例：量化要素清单必须是空的，而不是补一个"常见的"延迟小时数。"""
        services = bridge(scenario_provider=StubProvider([CASE_BARE]))
        result = await simulate(services, {"risk_level": 2, "hazard_type": "landslide"})

        scenario = result["scenarios"][0]
        assert scenario["impact_factors"] == []
        assert scenario["refs"] == [CASE_BARE["case_id"]]
        assert result["degraded"] is False

    async def test_没有case_id的命中被丢弃且不会被编一个id(self) -> None:
        """无出处的命中进不了结论；丢弃条数还要留在 `dropped_cases` 上，别让它静默消失。"""
        services = bridge(scenario_provider=StubProvider([CASE_NO_ID, CASE_WITH_NUMBERS]))
        result = await simulate(services, {"risk_level": 2})

        assert result["dropped_cases"] == 1
        assert result["case_count"] == 1
        ids = [case_id for scenario in result["scenarios"] for case_id in scenario["refs"]]
        assert ids == [CASE_WITH_NUMBERS["case_id"]]
        assert "12.0" not in str(result), "被丢弃案例里的数字不该出现在结论里"

    async def test_多条案例就是多个情景(self) -> None:
        services = bridge(scenario_provider=StubProvider([CASE_WITH_NUMBERS, CASE_BARE]))
        result = await simulate(services, {"risk_level": 3})

        assert [item["expected_level"] for item in result["scenarios"]] == [3, 3]
        assert {scenario["refs"][0] for scenario in result["scenarios"]} == {CASE_WITH_NUMBERS["case_id"], CASE_BARE["case_id"]}


class TestDegradedWithoutCases:
    async def test_没注入案例能力时显式降级并说明原因(self) -> None:
        result = await simulate(bridge(), {"risk_level": 2, "hazard_type": "rockfall"})

        assert result["degraded"] is True
        assert "未注入案例能力" in result["degraded_reason"] and "scenario_provider" in result["degraded_reason"]
        assert result["case_count"] == 0

    async def test_降级结论里没有任何案例出处与编造的量化数值(self) -> None:
        result = await simulate(bridge(), {"risk_level": 2})

        assert set(all_refs_and_provenance(result)) == {"payload"}, "降级结论只允许追溯到本次声明等级这一个来源"
        keys = {str(factor["key"]) for scenario in result["scenarios"] for factor in scenario["impact_factors"]}
        assert keys == {"declared_risk_level"}, f"降级时不该出现案例类要素：{sorted(keys)}"

    async def test_召回空命中同样降级而不是假装案例推演(self) -> None:
        provider = StubProvider([])
        result = await simulate(bridge(scenario_provider=provider), {"risk_level": 3})

        assert result["degraded"] is True
        assert "无命中" in result["degraded_reason"]
        assert provider.calls, "没问过案例能力就写下无命中，那是猜的"

    async def test_能力报错时降级且原因带着异常类型(self) -> None:
        """腿故障不能让整条链路炸掉，但也不能把"没问出来"读成"没事"：原因必须留在结论里。"""
        result = await simulate(bridge(scenario_provider=BoomProvider()), {"risk_level": 1})

        assert result["degraded"] is True
        assert "RuntimeError" in result["degraded_reason"]
        assert set(all_refs_and_provenance(result)) == {"payload"}

    async def test_降级情景仍然点名自己是降级来的(self) -> None:
        """引擎的 `_situation_simulate` 只把 scenarios 带进节点输出，所以"为什么"必须写在情景自己身上。"""
        result = await simulate(bridge(), {"risk_level": 4})

        assert {scenario["basis"].startswith("降级推演：") for scenario in result["scenarios"]} == {True}
        assert {tuple(scenario["refs"]) for scenario in result["scenarios"]} == {()}


class TestCanvasContractUnchanged:
    async def test_三个既有输出键与三档情景名保持不变(self) -> None:
        """画布读的是 scenarios/horizon_minutes/trend，降级分支的情景名也仍是保守/中性/不利——
        升级实现不能顺手改掉对外形状。"""
        result = await simulate(bridge(), {"risk_level": 2, "horizon_minutes": 90})

        assert {"horizon_minutes", "scenarios", "trend"} <= set(result)
        assert result["horizon_minutes"] == 90
        assert [scenario["name"] for scenario in result["scenarios"]] == ["保守", "中性", "不利"]
        assert [scenario["expected_level"] for scenario in result["scenarios"]] == [3, 2, 1]
        assert result["trend"] in {"升级", "持平", "缓解"}

    async def test_默认推演时长与案例条数走声明的默认值(self) -> None:
        result = await simulate(bridge(), {"risk_level": 3})

        assert result["horizon_minutes"] == 60
        provider = StubProvider([CASE_BARE, CASE_BARE, CASE_BARE, CASE_BARE, CASE_BARE, CASE_BARE])
        limited = await simulate(bridge(scenario_provider=provider), {"risk_level": 3})
        assert provider.calls[0]["limit"] == DEFAULT_SCENARIO_CASE_LIMIT
        assert limited["case_count"] == DEFAULT_SCENARIO_CASE_LIMIT

    @pytest.mark.parametrize("cases", [[CASE_WITH_NUMBERS], []], ids=["有案例", "无案例"])
    async def test_情景穿过节点handler后仍然带着出处与降级说明(self, cases: list[dict[str, Any]]) -> None:
        """引擎的 `_situation_simulate` 只把 `scenarios` 与 `horizon_minutes` 写进节点输出
        （顶层 `degraded` / `trend` 会被它丢掉），所以"这条结论有没有案例佐证"必须写在情景自己身上，
        否则画布上只剩一串看不出出处的等级数字。这条用例盯的就是那一步转换之后。"""
        services = bridge(scenario_provider=StubProvider(cases))
        outcome = (
            await default_registry()
            .require("situation_simulate")
            .handler(
                NodeContext(payload={"risk_level": 3, "hazard_type": "avalanche", "region_code": "540121"}, services=services),
                {},
            )
        )

        assert outcome.output["scenarios"], "节点输出不能是空情景"
        for scenario in outcome.output["scenarios"]:
            if cases:
                assert scenario["refs"] == [CASE_WITH_NUMBERS["case_id"]]
            else:
                assert scenario["refs"] == [] and scenario["basis"].startswith("降级推演：")


class TestInjectionContract:
    def test_新参数是可选关键字且默认不注入(self) -> None:
        """装配点不传时行为必须与升级前一致：这条是"没案例能力也照常跑"的结构性保证。"""
        parameter = inspect.signature(build_workflow_services).parameters["scenario_provider"]
        assert parameter.default is None
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    def test_协议是结构类型所以腿的实现无需继承(self) -> None:
        """内核只认方法形状：知识层/检索层/智能体代理都不用 import 本模块即可被注入。"""
        assert isinstance(StubProvider([]), ScenarioProvider)
        assert not isinstance(SimpleNamespace(), ScenarioProvider)

    def test_协议方法是异步且全部关键字参数(self) -> None:
        signature = inspect.signature(ScenarioProvider.recall_scenarios)
        assert inspect.iscoroutinefunction(ScenarioProvider.recall_scenarios)
        assert all(item.kind is inspect.Parameter.KEYWORD_ONLY for item in list(signature.parameters.values())[1:])
        assert {"hazard_type", "region_code", "risk_level", "limit"} >= set(list(signature.parameters)[1:])

    @pytest.mark.parametrize("provider", [None, StubProvider([CASE_WITH_NUMBERS])], ids=["未注入", "已注入"])
    async def test_两种装配下simulate都可用且输出形状一致(self, provider: Any) -> None:
        services = bridge(scenario_provider=provider)
        assert services.simulate is not None
        result = await simulate(services, {"risk_level": 3})

        assert {"horizon_minutes", "scenarios", "trend", "degraded", "degraded_reason"} <= set(result)
        assert result["scenarios"], "推演不能交出空情景列表"
        for scenario in result["scenarios"]:
            assert {"name", "expected_level", "impact_factors", "refs", "basis"} <= set(scenario)
            assert 1 <= int(scenario["expected_level"]) <= 5
