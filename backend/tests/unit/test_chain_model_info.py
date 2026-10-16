"""`model_info` 进链台账（§4.1 D10 + README 那句"可审计"的兑现处）。

三条口径，缺一条那句话就还是假的：
① 凭证只能从智能体的应答里**转记**，平台不许替它编一个模型名（那是伪造审计证据）；
② 本地降级腿没有模型参与 ⇒ 那一段【不带】这个键，而不是带一个 `null` 让人以为"走过模型但没记名"；
③ 带上了就要真的落到 `chain_runs.stages` 里——审计的意义是事后还能查，只在响应里过一遍不算。

这一组用假网关/假注册表驱动真链路：测的是链路怎么记账，不是 SDK 能不能跑（真库回合在
`tests/integration/test_persistence_postgres.py`，真智能体那一档见 docs/REPORT.md）。
"""

from __future__ import annotations

from typing import Any

from aegis.config import Settings
from aegis.domain.enums import AgentType, HazardType, RiskLevel
from aegis.domain.messages import ModelInfo, TelemetryReading, make_request, now_iso
from aegis.persistence import rows
from aegis.pipeline.result import ChainResult, StageResult, model_receipt
from aegis.pipeline.stages import HazardStages
from aegis.pipeline.workflow_chain import WorkflowChain
from conftest import chain_entry

REGION = "540121"
TRACE = "trc_" + "0" * 16
VERDICT_PAYLOAD = {
    "risk_level": int(RiskLevel.ORANGE),
    "hazard_type": HazardType.DEBRIS_FLOW.value,
    "confidence": 0.78,
    "rationale": "10 分钟降雨 28 毫米且泥位上行",
    "evidence_refs": ["R-DEBRIS-RAIN-1"],
}


def reading() -> TelemetryReading:
    return TelemetryReading(
        station_id="PG-S1",
        metric="rain_10min",
        value=28.0,
        unit="mm",
        region_code=REGION,
        observed_at=now_iso(),
        ingested_at=now_iso(),
        source="mqtt_edge",
    )


def _response(*, model_info: ModelInfo | None) -> Any:
    request = make_request(
        source="platform.t01",
        target="assess.stub",
        action="assess.hazard",
        payload={"region_code": REGION},
        trace_id=TRACE,
        reply_to="reply.t01.inbox",
    )
    from aegis.domain.messages import make_response

    message = make_response(request, source="assess.stub", payload=dict(VERDICT_PAYLOAD))
    return message.model_copy(update={"model_info": model_info}) if model_info is not None else message


class FakeRegistry:
    """只回答"这一段的智能体在不在场"，不参与任何业务判断。"""

    def __init__(self, present: set[str]) -> None:
        self._present = present

    def pick(self, agent_type: str, capability: str) -> object | None:
        return object() if agent_type in self._present else None


class FakeGateway:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[str] = []
        self.published: list[str] = []

    async def dispatch(self, agent_type: AgentType, action: Any, payload: dict[str, Any], **kwargs: Any) -> Any:
        self.calls.append(agent_type.value)
        return self._response

    async def publish_to(self, subject: str, message: Any) -> None:
        # 反馈段会广播一条事件；这里只留个痕，测的是"段上记没记账"，不是总线
        self.published.append(subject)


async def chain_with(response: Any | None) -> WorkflowChain:
    present = {"assess"} if response is not None else set()
    return await chain_entry(
        HazardStages(
            gateway=FakeGateway(response),  # type: ignore[arg-type]
            registry=FakeRegistry(present),  # type: ignore[arg-type]
            contracts=_NullContracts(),  # type: ignore[arg-type]
            settings=Settings(env="test", bus_backend="memory", store_backend="memory"),
        )
    )


class _NullContracts:
    """链路只在智能体真产出任务单元时问它校验；本组用例走的是本地剧本，不会被叫到。"""

    def stu_errors(self, payloads: list[dict[str, Any]]) -> list[str]:
        return []


def trigger_hit() -> Any:
    """直接给链路一对现成的触发命中（人工上报腿的入口形状）。

    为什么要绕开阈值：这一组用例测的是"应答里的凭证怎么记"，不该绑在某条规则的阈值数字上——
    阈值一调，这五个用例就会集体变成"没触发所以什么都没测"的假绿。
    """
    from aegis.domain.messages import TriggerHit

    return TriggerHit(
        rule_id="R-DEBRIS-RAIN-1",
        hazard_type=HazardType.DEBRIS_FLOW.value,
        region_code=REGION,
        score=0.8,
        evidence_refs=["rain_10min=28.0"],
        observed_at=now_iso(),
    )


async def _run(chain: WorkflowChain) -> ChainResult:
    return await chain.process([reading()], region_code=REGION, preset_hits=[trigger_hit()])


class TestCapture:
    async def test_智能体带了凭证就逐段转记(self) -> None:
        chain = await chain_with(_response(model_info=ModelInfo(model="deepseek-chat", model_version="v1", prompt_version="1.1.0")))
        result = await _run(chain)
        assess = result.stage("assess")
        assert assess is not None and assess.mode == "agent"
        assert assess.model_info == {"agent": "assess.stub", "model": "deepseek-chat", "model_version": "v1", "prompt_version": "1.1.0"}

    async def test_本地降级腿不带这个键而不是带一个空值(self) -> None:
        """`mode=local` 那几段根本没走模型：键缺席说的是"没这回事"，`null` 说的是"走了但没记名"。"""
        chain = await chain_with(None)
        result = await _run(chain)
        for stage in result.stages:
            assert "model_info" not in stage.as_dict(), stage.name
        assert result.stage_models == {}

    async def test_智能体没带凭证时不代填(self) -> None:
        chain = await chain_with(_response(model_info=None))
        result = await _run(chain)
        assess = result.stage("assess")
        assert assess is not None and assess.mode == "agent"
        assert assess.model_info is None and "model_info" not in assess.as_dict()

    async def test_规划产出被拒时不记凭证(self) -> None:
        """智能体答了但任务单元不可用 → 回落本地剧本，这一段就没有模型参与可言。

        少了这条，`if units and ...` 的守卫可以被摘掉而一切照绿——那时本地结果会被记到某个模型头上。
        """
        request = make_request(
            source="platform.t01",
            target="plan.stub",
            action="plan.stu",
            payload={"region_code": REGION},
            trace_id=TRACE,
            reply_to="reply.t01.inbox",
        )
        from aegis.domain.messages import make_response

        plan_response = make_response(request, source="plan.stub", payload={"task_units": []}).model_copy(
            update={"model_info": ModelInfo(model="deepseek-chat", prompt_version="1.0.0")}
        )

        class TwoAgentGateway(FakeGateway):
            async def dispatch(self, agent_type: AgentType, action: Any, payload: dict[str, Any], **kwargs: Any) -> Any:
                self.calls.append(agent_type.value)
                return plan_response if agent_type is AgentType.PLAN else self._response

        gateway = TwoAgentGateway(_response(model_info=ModelInfo(model="deepseek-chat", prompt_version="1.1.0")))
        chain = await chain_entry(
            HazardStages(
                gateway=gateway,  # type: ignore[arg-type]
                registry=FakeRegistry({"assess", "plan"}),  # type: ignore[arg-type]
                contracts=_NullContracts(),  # type: ignore[arg-type]
                settings=Settings(env="test", bus_backend="memory", store_backend="memory"),
            )
        )
        result = await chain.process([reading()], region_code=REGION, preset_hits=[trigger_hit()])
        assert "assess" in result.stage_models, gateway.calls
        assert "plan" not in result.stage_models, "采纳的是本地剧本，凭证不该跟着本地结果走"

    async def test_只有应答成功的那一段才记(self) -> None:
        """研判之外的段没有智能体在场，不许被顺手记上同一个凭证。"""
        chain = await chain_with(_response(model_info=ModelInfo(model="deepseek-chat", prompt_version="1.1.0")))
        result = await _run(chain)
        assert set(result.stage_models) == {"assess"}
        assert all(stage.model_info is None for stage in result.stages if stage.name != "assess")


class TestExecuteReceipt:
    """执行段：文案被采纳才记凭证；被数字闸拦回、保留平台模板时不记。"""

    async def test_文案进了预警才记模型凭证(self) -> None:
        result = await self._run_with_copy(body_zh="请即刻撤离沟口住户")  # 不含数字 → 过闸
        assert result.stage_models.get("execute"), result.stage_models
        assert result.stage("execute") is not None and result.stage("execute").model_info is not None

    async def test_文案全被数字闸拦回时不记(self) -> None:
        """智能体写的每个字段都含预警事实之外的数字 → 全部弃用、留平台模板。

        这时候记它的模型凭证就是把模板算到模型头上——与"规划回落本地剧本不记"是同一条判据。
        只拦回一部分（标题进了、正文弃用）时仍然记：进了预警的那几个字确实是这个模型写的。
        """
        partial = await self._run_with_copy(body_zh="已有 47 人遇难，请立即撤离")
        assert "execute" in partial.stage_models, "标题过闸进了预警，这一程确实有模型的文字参与"
        rejected = await self._run_with_copy(title_zh="已转移 1234 人", body_zh="已有 47 人遇难")
        assert "execute" not in rejected.stage_models, rejected.stage_models
        assert sum("弃用" in note for note in rejected.degradations) == 2, rejected.degradations

    async def _run_with_copy(self, *, title_zh: str = "泥石流风险预警", body_zh: str = "请即刻撤离沟口住户") -> ChainResult:
        from aegis.domain.messages import make_response

        request = make_request(
            source="platform.t01",
            target="execute.stub",
            action="execute.warn",
            payload={"region_code": REGION},
            trace_id=TRACE,
            reply_to="reply.t01.inbox",
        )
        copy_response = make_response(request, source="execute.stub", payload={"title_zh": title_zh, "body_zh": body_zh}).model_copy(
            update={"model_info": ModelInfo(model="deepseek-chat", prompt_version="1.1.0")}
        )

        class CopyGateway(FakeGateway):
            async def dispatch(self, agent_type: AgentType, action: Any, payload: dict[str, Any], **kwargs: Any) -> Any:
                self.calls.append(agent_type.value)
                return copy_response if agent_type is AgentType.EXECUTE else self._response

        assess_response = _response(model_info=ModelInfo(model="deepseek-chat", prompt_version="1.1.0"))
        chain = await chain_entry(
            HazardStages(
                gateway=CopyGateway(assess_response),  # type: ignore[arg-type]
                registry=FakeRegistry({"assess", "execute"}),  # type: ignore[arg-type]
                contracts=_NullContracts(),  # type: ignore[arg-type]
                settings=Settings(env="test", bus_backend="memory", store_backend="memory"),
            )
        )
        return await chain.process([reading()], region_code=REGION, preset_hits=[trigger_hit()])


class TestLedgerShape:
    def test_凭证随stages进台账行(self) -> None:
        """`chain_runs` 没有新列：凭证就在 `stages` 那个 jsonb 里——所以要直接验到行内容。"""
        result = ChainResult(trace_id=TRACE, event_id="evt_" + "0" * 12)
        result.stages.append(
            StageResult(
                "assess",
                "agent",
                True,
                12.0,
                "等级 2",
                model_info={"agent": "assess.real01", "model": "deepseek-chat", "prompt_version": "1.1.0"},
            )
        )
        row = rows.chain_row(result, finished_at=now_iso())
        stages = row[list(rows.CHAIN_COLUMNS).index("stages")]
        assert stages[0]["model_info"]["prompt_version"] == "1.1.0"
        assert len(rows.CHAIN_COLUMNS) == len(row), "列序与参数arity必须一致"

    def test_没有凭证的段不污染行形状(self) -> None:
        result = ChainResult(trace_id=TRACE, event_id="evt_" + "0" * 12)
        result.stages.append(StageResult("assess", "local", True, 1.0, "采用规则定级"))
        stage = rows.chain_row(result, finished_at=now_iso())[list(rows.CHAIN_COLUMNS).index("stages")][0]
        assert set(stage) == {"name", "mode", "ok", "latency_ms", "note"}, stage


class TestReceiptHelper:
    def test_缺子字段时只记有的(self) -> None:
        """`ModelInfo` 三个子字段都可选（缺省即未知）：转记时按 `exclude_none` 带走，不补占位字符串。"""
        response = _response(model_info=ModelInfo(model="deepseek-chat"))
        assert model_receipt(response) == {"agent": "assess.stub", "model": "deepseek-chat"}

    def test_完全没带凭证返回None(self) -> None:
        assert model_receipt(_response(model_info=None)) is None
