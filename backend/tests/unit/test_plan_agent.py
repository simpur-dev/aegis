"""plan 内环测试（§3.3）：LLM 精修基线 / reschedule 禁入 LLM / 缺位回落 / 基线偏离告警 / STU 100% 合法。

真 ContractRegistry 校验交付的每个 task_unit（stu_errors 必须空），把"STU 合法率 100%"钉成硬门禁。
假网关按脚本回 PlanRefinement，并记录 chat_structured 调用，用来证明 reschedule 一次都没进 LLM。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from aegis.agents.plan import PlanAdjustment, PlanAgent, PlanRefinement
from aegis.contracts import ContractRegistry
from aegis.domain.enums import AgentType, TaskType
from aegis.domain.messages import make_request
from aegis.services.llm.prompt_registry import PromptRegistry, PromptTemplate

TRACE = "trc_" + "6" * 16
EVENT = "evt_" + "a" * 12
RISK = {
    "hazard_type": "debris_flow",
    "region_code": "540121",
    "risk_level": 1,
    "confidence": 0.9,
    "rationale": "强降雨叠加泥位抬升",
    "evidence_refs": ["R-RAIN-1"],
}


class _NullTransport:
    name = "null"

    async def publish(self, *a: object, **k: object) -> None: ...


class FakePlanGateway:
    def __init__(self, refinement: PlanRefinement) -> None:
        self._refinement = refinement
        self.calls: list[str] = []
        self.available = True

    async def chat_structured(
        self, messages, *, schema, task="default", agent_id=None, trace_id=None, prompt_key=None, prompt_vars=None, **kw
    ):
        self.calls.append({"task": task, "messages": messages, "prompt_key": prompt_key, "prompt_vars": prompt_vars})
        return self._refinement

    def model_info_for_task(self, task):
        return {"model": "qwen-plus", "fallback_model": "", "task": task, "prompt_version": "1.0.0"}


def _request(action: str, risk: dict[str, Any] | None = None) -> Any:
    return make_request(
        source="platform.gateway",
        target="plan.real01",
        action=action,
        payload={
            "event_id": EVENT,
            "risk": risk or RISK,
            "objective": "处置剧本",
            "constraints": {},
            "reference_cases": [],
            "retrieved_context": [],
        },
        trace_id=TRACE,
        reply_to="reply.gw0000000c.inbox",
        deadline_ms=5000,
    )


def agent(gw: Any = None, *, contracts: ContractRegistry | None = None) -> PlanAgent:
    return PlanAgent(
        transport=_NullTransport(),
        agent_type=AgentType.PLAN,
        agent_id="plan.real01",
        capabilities=["task_decompose"],
        llm=gw,
        contracts=contracts,
    )


class TestSchemaBounds:
    def test_unknown_task_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PlanAdjustment(task_type="party", objective="x", priority=3, sla_seconds=100)

    def test_out_of_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PlanAdjustment(task_type="warn", objective="x", priority=9, sla_seconds=100)


class TestRefinement:
    async def test_llm_refines_baseline_and_stays_contract_legal(self, contracts: ContractRegistry) -> None:
        gw = FakePlanGateway(
            PlanRefinement(adjustments=[PlanAdjustment(task_type="warn", objective="面向民众的简明预警", priority=1, sla_seconds=90)])
        )
        reply = await agent(gw, contracts=contracts).handle(_request("plan.stu"))
        units = reply.payload["task_units"]
        assert units, "必须产出 STU"
        # 正文走 PromptRegistry（单一真源）：交 prompt_key + 变量，不内联提示词。
        assert gw.calls[0]["prompt_key"] == "plan"
        assert gw.calls[0]["messages"] == []
        assert "灾种：debris_flow" in gw.calls[0]["prompt_vars"]["verdict_line"]
        assert contracts.stu_errors(units) == [], "交付的每个 STU 必须 100% 合法"
        assert reply.payload["llm_decided"] is True
        assert reply.payload["source"] == "llm_refined"
        warn_unit = next(u for u in units if u["task_type"] == "warn")
        assert warn_unit["objective"] == "面向民众的简明预警"
        assert warn_unit["sla_seconds"] == 90
        assert warn_unit["created_by"] == "plan.real01"
        assert all(u["created_by"] == "plan.real01" for u in units)

    async def test_deviations_recorded_not_injected(self, contracts: ContractRegistry) -> None:
        # ROCKFALL 基线无 evacuate（转移依赖已知威胁区），LLM 若建议则记偏离、不凭空塞入覆盖。
        rockfall = {
            "hazard_type": "rockfall",
            "region_code": "540121",
            "risk_level": 1,
            "confidence": 0.9,
            "rationale": "危石失稳",
            "evidence_refs": ["R-ROCK-1"],
        }
        gw = FakePlanGateway(
            PlanRefinement(adjustments=[PlanAdjustment(task_type="evacuate", objective="组织转移", priority=1, sla_seconds=600)])
        )
        reply = await agent(gw, contracts=contracts).handle(_request("plan.stu", rockfall))
        types = {u["task_type"] for u in reply.payload["task_units"]}
        assert "evacuate" not in types, "基线外 task_type 不得被凭空塞入"
        assert any("evacuate" in d for d in reply.payload["deviations"])
        assert contracts.stu_errors(reply.payload["task_units"]) == []


class TestNoLLMPaths:
    async def test_reschedule_never_calls_llm(self, contracts: ContractRegistry) -> None:
        gw = FakePlanGateway(PlanRefinement(adjustments=[]))
        reply = await agent(gw, contracts=contracts).handle(_request("plan.reschedule"))
        assert gw.calls == [], "plan.reschedule 禁入 LLM（§3.3 ≤10s 硬约束）"
        assert reply.payload["llm_decided"] is False
        assert reply.payload["source"] == "local_playbook"
        assert contracts.stu_errors(reply.payload["task_units"]) == []

    async def test_missing_llm_falls_back_to_local_playbook(self, contracts: ContractRegistry) -> None:
        reply = await agent(None, contracts=contracts).handle(_request("plan.stu"))
        assert reply.payload["source"] == "local_playbook"
        assert reply.payload["llm_decided"] is False
        assert contracts.stu_errors(reply.payload["task_units"]) == []

    async def test_unavailable_llm_falls_back(self, contracts: ContractRegistry) -> None:
        gw = FakePlanGateway(PlanRefinement(adjustments=[]))
        gw.available = False
        reply = await agent(gw, contracts=contracts).handle(_request("plan.stu"))
        assert gw.calls == []
        assert reply.payload["source"] == "local_playbook"


async def test_提示词里给出本次可精修的task_type白名单() -> None:
    """只说"取枚举之一"而不给清单，模型会交回"交通管制"这类中文标签，schema 整批拒收
    （真机实测 3 次 MalformedLlmOutputError → 决策降级）。白名单还给窄不给宽：
    只列本地剧本这次真产出的类型，不诱导模型去精修一条本次不存在的步骤。"""
    gateway = FakePlanGateway(PlanRefinement(adjustments=[]))
    await agent(gateway, contracts=ContractRegistry()).handle(_request("plan.stu"))
    variables = gateway.calls[0]["prompt_vars"]
    whitelist = variables["task_types"]
    assert whitelist, "白名单槽位必须是实数，不能是空串"
    tokens = [t.strip() for t in whitelist.replace(",", "、").split("、") if t.strip()]
    assert tokens, f"白名单解析不出任何类型：{whitelist!r}"
    assert set(tokens) <= set(TaskType._value2member_map_), f"白名单里出现了枚举之外的名称：{tokens}"


def test_模板正文必须留白名单槽位() -> None:
    """正文单一真源在注册表：槽位漏了就等于把那条腿退回"靠模型猜枚举"的不可用状态。"""
    template = PromptRegistry.builtin()._by_task["plan"]
    assert "{task_types}" in template.user
    assert "逐字取自给定白名单" in template.system
    assert template.version == "1.1.0", "提示词正文变了就必须换版本号（D10：结论里的 prompt_version 要能对上正文）"
    with pytest.raises(ValueError):
        PromptRegistry([PromptTemplate(task="plan", version="9.9.9", system="s", user="白名单：{task_types}")]).build("plan")
