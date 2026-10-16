"""assistant 意图解构引擎测试（§4.7 / R6）：白名单动作链分解、越权剔除、无 LLM 不臆造。"""

from __future__ import annotations

from typing import Any

from aegis.config import Settings
from aegis.services.interpret.assistant import AssistantService, PlanDecomposition, PlanStep


class StubLLM:
    def __init__(self, decomp: PlanDecomposition) -> None:
        self._d = decomp
        self.available = True
        self.calls: list[Any] = []

    async def chat_structured(self, messages, *, schema, task="default", prompt_key=None, prompt_vars=None, temperature=None, **kw):
        self.calls.append({"task": task, "prompt_key": prompt_key, "prompt_vars": prompt_vars})
        return self._d


def service(decomp: PlanDecomposition | None = None) -> tuple[AssistantService, StubLLM | None]:
    if decomp is None:
        return AssistantService(llm=None, settings=Settings(env="test")), None
    llm = StubLLM(decomp)
    return AssistantService(llm=llm, settings=Settings(env="test")), llm


class TestProposePlan:
    async def test_decomposes_to_whitelisted_chain_and_drops_offlist(self) -> None:
        decomp = PlanDecomposition(
            steps=[
                PlanStep(action="query.warnings", reason="先看当前预警"),
                PlanStep(action="delete.everything", reason="越权动作"),
                PlanStep(action="run.drill", reason="准备演练"),
            ]
        )
        svc, llm = service(decomp)
        result = await svc.propose_plan("看看波密有没有风险，有的话准备演练", region_code="540121")
        actions = [s["action"] for s in result["steps"]]
        assert actions == ["query.warnings", "run.drill"], "越权动作被剔除，只留白名单链"
        assert result["rejected"] == ["delete.everything"]
        assert result["decided_by"] == "llm"
        # 每步都带 requires_confirmation 字段（LLM 只有规划权，执行仍交人工确认闸）。
        assert all("requires_confirmation" in s for s in result["steps"])
        drill = next(s for s in result["steps"] if s["action"] == "run.drill")
        assert drill["requires_confirmation"] is True
        assert llm is not None and llm.calls[0]["prompt_key"] == "assistant"

    async def test_no_llm_returns_empty_plan_not_fabricated(self) -> None:
        svc, _ = service(None)
        result = await svc.propose_plan("随便说点什么")
        assert result["steps"] == []
        assert result["decided_by"] == "none"

    async def test_llm_error_degrades_to_empty_plan(self) -> None:
        class Boom(StubLLM):
            async def chat_structured(self, *a, **k):
                raise RuntimeError("网关挂了")

        llm = Boom(PlanDecomposition())
        svc = AssistantService(llm=llm, settings=Settings(env="test"))
        result = await svc.propose_plan("x")
        assert result["steps"] == []
        assert "降级" in result["note"]
