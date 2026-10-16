"""§4.4 的装配面：推演沙盒接进 DAG 节点 `situation_simulate`，以及它塌掉时的形状。

节点对外契约不许变（画布在读 `scenarios`/`horizon_minutes`/`trend`），变的是能力：沙盒那份
K×T 概率矩阵、角色票与预案对比要进 `sandbox` 这一格。四件事必须钉住：

1. 沙盒缺位/报错只降级 `sandbox` 那一格——"情景有了"不许冒充"推演跑过了"；
2. 装配点（`container._sandbox_runner`）交出来的确实是跑到底的报告，而不是协程对象
   （`await x.run(cfg).as_dict()` 这种少括号写法运行期才炸，所以要用真装配跑一遍）；
3. 画布上人手填的整数参数全部夹进区间：`scenarios=1e9` 不该变成一次跑不完的循环；
4. 角色票在节点输出里看得见（`timeline[].actions` + `applied_playbook`），否则报告页无从渲染。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from aegis.config import Settings
from aegis.container import _sandbox_runner
from aegis.services.simulation import SimConfig
from aegis.workflow.nodes import NodeContext, WorkflowServices, default_registry
from aegis.workflow.services_bridge import build_workflow_services

BRIDGE_ARGS: dict[str, Any] = {
    "store": SimpleNamespace(),
    "rule_engine": SimpleNamespace(),
    "risk_engine": SimpleNamespace(),
    "warning_service": SimpleNamespace(),
    "dispatcher": SimpleNamespace(),
    "gateway": SimpleNamespace(),
}
#: 案例腿给一条真带 case_id 的案例：这样顶层 `degraded` 是 False，`sandbox` 的降级才是独立事实。
CASE = {"case_id": "case_sandbox_leg_01", "title": "泥位上涨下的转移预案", "applies_to_levels": [3, 4]}

CASE_REPORT: dict[str, Any] = {
    "cost": {"requested_scenarios": 3, "effective_scenarios": 3, "downscaled": False},
    "probability_matrix": [{"rain_bucket_mm": "40-50", "scenarios": 3, "mean_secondary_rate": 0.4, "max_secondary_rate": 0.5}],
    "timeline": [{"round": 0, "actions": {"commander": "转移"}, "applied_playbook": "转移", "trigger_probability": 0.4}],
    "playbook_comparison": [{"playbook": "转移", "mitigation": 0.6}],
    "roles": {"policy": "rule", "asked": 4, "calls": 4, "fallbacks": 0},
}


class _Cases:
    async def recall_scenarios(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [dict(CASE)]


class RecordingRunner:
    def __init__(self, report: dict[str, Any] | None = None, *, raises: bool = False) -> None:
        self.report = dict(CASE_REPORT if report is None else report)
        self.raises = raises
        self.payloads: list[dict[str, Any]] = []

    async def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        if self.raises:
            raise TimeoutError("沙盒腿超时")
        return self.report


def bridge(**overrides: Any) -> WorkflowServices:
    return build_workflow_services(**{**BRIDGE_ARGS, "scenario_provider": _Cases(), **overrides})


async def simulate(services: WorkflowServices, payload: dict[str, Any]) -> dict[str, Any]:
    assert services.simulate is not None
    return await services.simulate(payload)


async def run_node(services: WorkflowServices, payload: dict[str, Any]) -> dict[str, Any]:
    outcome = await default_registry().require("situation_simulate").handler(NodeContext(payload=payload, services=services), {})
    return outcome.output


class TestSandboxCell:
    async def test_未注入沙盒时只有sandbox那一格降级(self) -> None:
        """案例腿绿、沙盒缺位：顶层 `degraded` 仍是 False，缺位只写在 `sandbox` 里并点名参数。"""
        result = await simulate(bridge(), {"risk_level": 3})

        assert result["degraded"] is False
        assert result["sandbox"]["degraded"] is True
        assert "sandbox_runner" in result["sandbox"]["degraded_reason"]
        assert result["scenarios"], "情景不受沙盒缺位影响"

    async def test_报告原样进那一格(self) -> None:
        runner = RecordingRunner()
        result = await simulate(bridge(sandbox_runner=runner), {"risk_level": 3, "playbook": "封路"})

        assert result["sandbox"] == CASE_REPORT
        assert result["degraded"] is False, "沙盒报告不该把案例腿的绿改写掉"
        assert runner.payloads[0]["risk_level"] == 3, "payload 要原样递给沙盒，画布参数才有落点"

    async def test_沙盒报错不炸节点也不改情景(self) -> None:
        """增强腿失败收口在增强腿（与 §3.2 双盲同一口径），但原因必须留在结论里。"""
        result = await simulate(bridge(sandbox_runner=RecordingRunner(raises=True)), {"risk_level": 2})

        assert result["sandbox"]["degraded"] is True
        assert "TimeoutError" in result["sandbox"]["degraded_reason"]
        assert [scenario["name"] for scenario in result["scenarios"]] == ["泥位上涨下的转移预案"]
        assert result["degraded"] is False

    def test_sandbox_runner是可选关键字参数且默认不注入(self) -> None:
        parameter = inspect.signature(build_workflow_services).parameters["sandbox_runner"]
        assert parameter.default is None
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


class TestNodeCarriesSandbox:
    async def test_handler把sandbox带进节点输出(self) -> None:
        output = await run_node(bridge(sandbox_runner=RecordingRunner()), {"risk_level": 3, "hazard_type": "debris_flow"})

        assert output["sandbox"] == CASE_REPORT
        assert output["scenarios"] and output["horizon_minutes"] == 60

    async def test_handler在没有沙盒时交出降级格而不是null(self) -> None:
        """节点输出留 null 就等于"这条推演没跑沙盒"在画布上不可见（铁律 7）。"""
        output = await run_node(bridge(), {"risk_level": 3})

        assert output["sandbox"]["degraded"] is True
        assert output["sandbox"]["degraded_reason"]


class TestAssemblyRunner:
    """真装配那一段：少一对括号、漏夹一个参数，都该在这里红而不是在运行时报错。"""

    async def test_默认配置跑出规则档报告与四角色票(self) -> None:
        report = await _sandbox_runner(Settings(env="test"), None)({"risk_level": 3})

        assert {"cost", "probability_matrix", "timeline", "playbook_comparison", "roles"} <= set(report)
        assert report["roles"]["policy"] == "rule"
        assert report["probability_matrix"]
        for row in report["timeline"]:
            assert set(row["actions"]) == set(SimConfig().roles)
            assert row["applied_playbook"] in {"转移", "封路", "不行动"}

    async def test_同参数同种子逐键可复现(self) -> None:
        runner = _sandbox_runner(Settings(env="test"), None)
        assert await runner({"seed": 7}) == await runner({"seed": 7})

    async def test_scenarios_超上限被夹住并且留痕(self) -> None:
        report = await _sandbox_runner(Settings(env="test"), None)({"scenarios": 10**9})

        assert report["cost"]["requested_scenarios"] == 64, "夹在装配点做，报告里就记夹后的值"
        assert report["cost"]["effective_scenarios"] <= 64

    async def test_rounds_超上限决定时间线长度(self) -> None:
        report = await _sandbox_runner(Settings(env="test"), None)({"rounds": 10**9})
        assert len(report["timeline"]) == 24

    async def test_token_budget_不许越过默认上限(self) -> None:
        report = await _sandbox_runner(Settings(env="test"), None)({"token_budget": 10**9})
        assert report["cost"]["effective_scenarios"] == SimConfig().scenarios
        assert report["cost"]["downscaled"] is False

    async def test_负数参数落到各自下限而不是抛错(self) -> None:
        report = await _sandbox_runner(Settings(env="test"), None)({"scenarios": -5, "rounds": -5, "token_budget": -5})
        assert report["cost"]["requested_scenarios"] == 1
        assert len(report["timeline"]) == 1
        assert report["cost"]["effective_scenarios"] == 1

    async def test_开关开但网关不在位时报告说的是规则档(self) -> None:
        """不假装接了模型：`roles.policy` 与实际用的那一档同源。"""
        report = await _sandbox_runner(Settings(env="test", simulation_roles_llm=True), None)({"risk_level": 3, "rounds": 1})
        assert report["roles"]["policy"] == "rule"

    @pytest.mark.parametrize("rain", [0, 120.5], ids=["零降雨", "极端降雨"])
    async def test_雨强由画布参数决定并写进时间线(self, rain: float) -> None:
        report = await _sandbox_runner(Settings(env="test"), None)({"rain_base_mm": rain, "rounds": 1})
        row = report["timeline"][0]
        assert row["rain_mm"] <= rain + SimConfig().rain_spread_mm
        assert row["mudlevel_cm"] >= 0.0
