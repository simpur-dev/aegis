"""意图解构引擎测试：注册表内动作链分解、越权剔除、无 LLM 不臆造。

多步解构挂在 `IntentClassifier` 上（不是独立端点）：`/assistant/plan` 已退役，
将来由 `/chat` 的 `plan` 帧承载，判定逻辑不需要第二个入口。
"""

from __future__ import annotations

from typing import Any

from aegis.services.assistant import ActionDomain, ActionRegistry
from aegis.services.assistant.actions.control import DrillStart
from aegis.services.assistant.actions.query import WarningList
from aegis.services.assistant.intent import IntentClassifier, PlanDecomposition, PlanStep


class _Warnings:
    def list(self, *, limit: int = 50, region_code: str | None = None, released: bool | None = None) -> list[Any]:
        return []

    def get(self, warning_id: str) -> Any | None:
        return None


def build_registry() -> ActionRegistry:
    async def run_drill(*, scenario: str = "surge", ticks: int = 2, region_code: str | None = None) -> dict[str, Any]:
        return {"chains": [], "regions": 0}

    registry = ActionRegistry(
        [WarningList(_Warnings()), DrillStart(run_drill, available=True)],
        domains={ActionDomain.WARNING, ActionDomain.DRILL},
    )
    registry.assert_consistent()
    return registry


class StubLLM:
    def __init__(self, decomp: PlanDecomposition) -> None:
        self._d = decomp
        self.available = True
        self.calls: list[Any] = []

    async def chat_structured(self, messages, *, schema, task="default", prompt_key=None, prompt_vars=None, temperature=None, **kw):
        self.calls.append({"task": task, "prompt_key": prompt_key, "prompt_vars": prompt_vars})
        return self._d


def classifier(decomp: PlanDecomposition | None = None) -> tuple[IntentClassifier, StubLLM | None]:
    if decomp is None:
        return IntentClassifier(build_registry(), llm=None), None
    llm = StubLLM(decomp)
    return IntentClassifier(build_registry(), llm=llm), llm


class TestProposePlan:
    async def test_decomposes_to_registered_chain_and_drops_unknown(self) -> None:
        decomp = PlanDecomposition(
            steps=[
                PlanStep(action="warning.list", reason="先看当前预警"),
                PlanStep(action="delete.everything", reason="越权动作"),
                PlanStep(action="drill.start", reason="准备演练"),
            ]
        )
        judge, llm = classifier(decomp)
        result = await judge.propose_plan("看看波密有没有风险，有的话准备演练", region_code="540121")
        actions = [s["action"] for s in result["steps"]]
        assert actions == ["warning.list", "drill.start"], "未注册动作被剔除，只留注册表内的链"
        assert result["rejected"] == ["delete.everything"]
        assert result["decided_by"] == "llm"
        assert all("requires_confirmation" in s for s in result["steps"])
        assert llm is not None and llm.calls[0]["prompt_key"] == "assistant"

    async def test_no_llm_returns_empty_plan_not_fabricated(self) -> None:
        judge, _ = classifier(None)
        result = await judge.propose_plan("随便说点什么")
        assert result["steps"] == []
        assert result["decided_by"] == "none"

    async def test_llm_error_degrades_to_empty_plan(self) -> None:
        class Boom(StubLLM):
            async def chat_structured(self, *a, **k):
                raise RuntimeError("网关挂了")

        judge = IntentClassifier(build_registry(), llm=Boom(PlanDecomposition()))
        result = await judge.propose_plan("x")
        assert result["steps"] == []
        assert "降级" in result["note"]
