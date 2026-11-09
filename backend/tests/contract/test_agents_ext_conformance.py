"""外部交付实现（`aegis.agents_ext`）过《智能体接入规范》§7 的 7 项一致性门禁。

平台侧那套门禁有两条腿：`test_agent_conformance.py`（Mock 驱动的通用样例，兼平台的准入闸门）
与 `test_real_agents_conformance.py`（平台自建内环过同一套断言）。外部实现并入后**没有**过这套门禁——
评审把它记成缺口。本文件补上第三条腿：**外部实现过同一套 7 项断言**。

## 为什么要独立成文件，而不是并进 Mock 那套
Mock 那套判的是"平台认不认得违约行为"，本文件判的是"**外部实现本身**合不合契约"。
两者的断言口径必须一致（同一个 `Action`、同一本事务台账、同一套错误码），
否则"过了门禁"这句话没有意义——各判各的，两边都绿，缺口照样在。

## 只接了一个动作这件事，在门禁里怎么如实表达
外部实现目前只接线了 `assess.hazard`，其余动作（`plan.stu` / `execute.warn` / `feedback.attribute`）
按契约回 `E_EXT_ACTION_UNSUP` 的错误回执。**门禁不许被放宽成"没接线也算过"**：
- 凡是"逐个动作过一遍"的断言，外部实现只覆盖 `assess.hazard`；
- 未接线的动作断言的是**另一条契约要求**：必须以类型化错误回执拒绝，且不得产生成功回执
  （否则平台会把一条没做的动作记成"协同成功"，协同成功率虚高——评审抓过这个形状）；
- 第 7 项（STU 上下文快照）在外部实现上**覆盖不到**：它没有 STU 产出，钉它就得先接线。
  本文件只钉"未接线的决策段不伪造快照"，并在类注释里写明缺口。

## 假 runner 的边界
只替身**最里层那一腿**（她的 LangGraph 图与 LLM 调用）：门禁要判的是
"适配层怎么把她的结果变成平台契约消息"，不是她那套图跑不跑得起来。
适配层本身是真身（`ExternalAgentAdapter`），总线与网关也都是真的；
假 runner 的入口签名由 `test_适配层调用的入口签名与真实实现一致` 对着真实现钉住，
免得这个替身测的是一个虚构接口。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import time
from pathlib import Path
from typing import Any

import pytest

from aegis.agents_ext import adapter as adapter_module
from aegis.agents_ext.adapter import ExternalAgentAdapter, build_ext_agents
from aegis.agents_ext.levels import CODE_ACTION_UNSUPPORTED, SOURCE_MARK
from aegis.bus import subjects
from aegis.bus.gateway import AgentGateway
from aegis.bus.inmemory import InMemoryBus
from aegis.bus.registry import AgentRegistry
from aegis.domain.enums import Action, AgentType, HazardType, MessageKind, RiskLevel
from aegis.domain.messages import AgentMessage, make_request
from aegis.errors import AegisError

TRACE = "trc_" + "5" * 16
REGION = "540121"

#: 外部实现的包根：门禁 6 的静态检查扫这里。
EXT_PACKAGE = Path(__file__).resolve().parents[2] / "src" / "aegis" / "agents_ext"


def _risk_result(**overrides: Any) -> dict[str, Any]:
    """外部实现的 `risk_result` 形状（按 `risk_output_node` 的产出裁剪）。

    **与真的实现差一处**：`confidence` —— 她的 risk_result 里**没有**这个字段
    （因子引擎给的是 `provisional_level`，不是置信度），而平台契约要求 confidence。
    这条缺口由 `test_她的结果缺置信度时不得产出定级` 单独钉住；这里补上它，
    是为了让"结论合法"这一条能被单独验（否则本文件一条正向断言都写不出来）。
    """
    payload: dict[str, Any] = {
        "status": "success",
        "hazard_type": "debris_flow",
        "risk_level": "high",
        "confidence": 0.8,
        "reasoning": "因子引擎输出：P=0.72，T=0.61（R=44.0）",
        "evidence": [{"statement": "孕灾条件得分 P=0.72", "source": "factor_engine", "type": "observation"}],
        "model_info": {"rule_version": "FACTOR-ENGINE-v0.1", "is_mock": True},
    }
    payload.update(overrides)
    return payload


def _assess_payload(hazards: list[str] | None = None) -> dict[str, Any]:
    """平台往研判段下的单（§3：`region_code` + `candidate_hazards` + 规则命中）。"""
    return {
        "region_code": REGION,
        "window": "1h",
        "candidate_hazards": hazards if hazards is not None else ["debris_flow"],
        "hits": [{"rule_id": "R-DEBRIS-RAIN-1", "hazard_type": "debris_flow", "region_code": REGION, "score": 0.9}],
    }


class FakeExtRunner:
    """外部实现入口（`run_risk_agent`）的最小替身。

    **只替最里层那一腿**（她的 LangGraph 图与 LLM 调用）：门禁要判的是
    "适配层怎么把她的结果变成平台契约消息"，不是她那套图跑不跑得起来。

    记下每次调用的入参：灾种键两套名字的映射那一项要靠它看
    "平台给的键"与"她收到的键"是不是同一个世界（映射错了她的结论 100% 被丢弃）。
    """

    def __init__(self, result: dict[str, Any] | BaseException | None = None, *, delay_s: float = 0.0) -> None:
        self.result: dict[str, Any] | BaseException = _risk_result() if result is None else result
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, task: str, data_result: dict[str, Any], hazard_type: str, **_: Any) -> dict[str, Any]:
        self.calls.append({"task": task, "data_result": data_result, "hazard_type": hazard_type})
        if self.delay_s:
            # 她的入口是同步的：这里阻塞的是工作线程，不是事件循环（适配层用 to_thread 跑它）。
            time.sleep(self.delay_s)
        if isinstance(self.result, BaseException):
            raise self.result
        return dict(self.result)


@pytest.fixture
def ext_runner(monkeypatch: pytest.MonkeyPatch) -> FakeExtRunner:
    """把适配层的运行器登记表换成假 runner（只替最里层那一腿）。

    为什么换而不是直接跑她的真图：她的图要真 LLM（没 Key 时每次调用都 401 兜底），
    而门禁要判的是**契约面**。换掉的是运行器登记表，`ExternalAgentAdapter` 本身
    （订阅 / 心跳 / 幂等 / 超时 / 错误回执）全是真身。
    """
    runner = FakeExtRunner()
    monkeypatch.setattr(adapter_module, "_load", lambda: {"risk": runner})
    return runner


async def bring_up_ext(bus: InMemoryBus, gateway: AgentGateway, agent_type: AgentType) -> ExternalAgentAdapter:
    """把外部实现的某一个本体挂上网关（走真的 `build_ext_agents` 接线表）。"""
    agents = build_ext_agents(bus)
    agent = next(item for item in agents if item.agent_type is agent_type)
    assert isinstance(agent, ExternalAgentAdapter)
    await gateway.start()
    await agent.start(heartbeat_interval=0.05)
    await bus.idle()
    await asyncio.sleep(0.01)
    await bus.idle()
    return agent


async def collect_receipts(bus: InMemoryBus, request: AgentMessage) -> list[AgentMessage]:
    """订阅回执口，把这一条请求的回执照单收齐（要分清"回了几封、分别是什么 kind"）。"""
    receipts: list[AgentMessage] = []

    async def collector(message: AgentMessage) -> None:
        receipts.append(message)

    await bus.subscribe(request.reply_to or "", collector)
    return receipts


async def wait_for_receipts(bus: InMemoryBus, receipts: list[AgentMessage], *, count: int = 1, timeout_s: float = 3.0) -> None:
    """等回执到齐。

    等的是"该来的来了"，不是"固定睡够"——固定睡眠在慢机器上会假红，
    在快机器上又白等（这份门禁要在 CI 与本机都稳）。
    """
    waited = 0.0
    while len(receipts) < count and waited < timeout_s:
        await asyncio.sleep(0.02)
        await bus.idle()
        waited += 0.02


async def settle(bus: InMemoryBus, seconds: float = 0.15) -> None:
    """静置一小会儿再数回执：多来一封回执这种事要给它机会发生，否则"只回一封"是假绿。"""
    await asyncio.sleep(seconds)
    await bus.idle()


class TestGate1SchemaLegality:
    """门禁 1：所有上行消息 100% 通过 `agent_message.v1` 校验。

    外部实现只接线了 `assess.hazard`，所以"逐个动作过一遍"在这里覆盖的是两件事：
    - 已接线的 `assess.hazard` → 回执必须是一封合法的 response，且字段齐全；
    - 未接线的动作 → 回执必须是一封合法的 **error**，而不是一封长得像成功的 response。
    """

    async def test_研判回执过契约且字段齐全(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        reply = await gateway.dispatch(
            AgentType.ASSESS,
            Action.ASSESS_HAZARD,
            _assess_payload(),
            trace_id=TRACE,
            capability="risk_assess",
        )
        assert gateway.counters["rejected"] == 0, "外部实现的回执必须过 agent_message.v1"
        assert reply.kind is MessageKind.RESPONSE
        assert reply.trace_id == TRACE
        assert reply.causation_id is not None
        assert reply.payload["risk_level"] in [int(level) for level in RiskLevel]
        # 规范 §3：`assess.risk_level` 的必填字段一个都不能少
        for field in ("hazard_type", "region_code", "risk_level", "confidence", "rationale", "evidence_refs"):
            assert field in reply.payload, f"研判回执缺字段：{field}"
        assert reply.payload["source"] == SOURCE_MARK, "数据出处必须外标：外部实现的数据是内存 Mock 源"
        # D10：溯源字段缺省即未知——不臆造模型版本
        assert reply.model_info is not None
        assert reply.model_info.model_version is None and reply.model_info.prompt_version is None
        await agent.stop()
        await gateway.close()

    @pytest.mark.parametrize(
        "agent_type,action,capability,payload",
        [
            (AgentType.PLAN, Action.PLAN_STU, "task_decompose", {"event_id": "evt_" + "a" * 12, "risk": {}}),
            (
                AgentType.EXECUTE,
                Action.EXECUTE_WARN,
                "warn_publish",
                {"warning_id": "wrn_1", "channels": ["sms"], "audiences": ["residents"]},
            ),
            (AgentType.FEEDBACK, Action.FEEDBACK_ATTRIBUTE, "reach_track", {"warning_id": "wrn_1"}),
        ],
    )
    async def test_未接线的动作以类型化错误回执拒绝(
        self,
        bus: InMemoryBus,
        gateway: AgentGateway,
        registry: AgentRegistry,
        ext_runner: FakeExtRunner,
        agent_type: AgentType,
        action: Action,
        capability: str,
        payload: dict,
    ) -> None:
        """没接线的动作不许"猜着做"，也不许静默：必须回一封类型化错误，且不得记成协同成功。"""
        agent = await bring_up_ext(bus, gateway, agent_type)
        with pytest.raises(AegisError) as info:
            await gateway.dispatch(agent_type, action, payload, trace_id=TRACE, capability=capability)
        assert info.value.detail["code"] == CODE_ACTION_UNSUPPORTED
        assert gateway.counters["rejected"] == 0, "错误回执本身也要过契约（否则网关拒收，平台看不到原因）"
        assert gateway.transactions[-1].outcome == "error"
        assert gateway.transactions[-1].error_code == CODE_ACTION_UNSUPPORTED
        assert gateway.success_rate() == 0.0, "没做的动作不许记成协同成功"
        await agent.stop()
        await gateway.close()

    async def test_感知段回执形状不合时平台降级而不是采纳(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        """感知段（PERCEIVE）**明确不接线**：平台那一段要的形状是 `trigger_hits`。

        外部实现的研判翻译给的是定级结论，给不了 trigger_hits。接了再让平台"判形状不合
        再回落"看着像降级、其实是接线失败——所以装配层直接把这一段标成未接线，
        请求会被**类型化错误**拒绝（走 error 通道、事务记 error、不计协同成功）。
        """
        agent = await bring_up_ext(bus, gateway, AgentType.PERCEIVE)
        # 网关收到 error 回执会抛 AegisError（既定行为：平台据此走规则降级轨）
        with pytest.raises(AegisError) as exc:
            await gateway.dispatch(
                AgentType.PERCEIVE,
                Action.ASSESS_HAZARD,
                {"region_code": REGION, "window": "1h", "candidate_hazards": ["debris_flow"], "readings": []},
                trace_id=TRACE,
                capability="anomaly_detect",
            )
        assert exc.value.detail.get("code") == CODE_ACTION_UNSUPPORTED
        assert gateway.transactions[-1].outcome == "error", "未接线不许记成协同成功"
        assert gateway.success_rate() == 0.0
        await agent.stop()
        await gateway.close()


class TestGate2Idempotency:
    """门禁 2：同一 `msg_id` 重复投递不产生重复副作用。"""

    async def test_重复请求只回一封(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        request = make_request(
            source="platform.gateway",
            target=agent.agent_id,
            action=Action.ASSESS_HAZARD.value,
            payload=_assess_payload(),
            trace_id=TRACE,
            reply_to="reply.gwext0001.inbox",
            deadline_ms=800,
        )
        receipts = await collect_receipts(bus, request)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)  # 重复投递
        await bus.idle()
        await wait_for_receipts(bus, receipts)
        await settle(bus)

        assert len(receipts) == 1, "重复 msg_id 不得产生第二次响应"
        assert agent.duplicates_skipped == 1
        assert len(ext_runner.calls) == 1, "重复投递不得让外部实现多跑一次"
        await agent.stop()
        await gateway.close()


class TestGate3TimeoutSemantics:
    """门禁 3：延迟响应 > `deadline_ms` 时平台正确记 `E_TIMEOUT`，且智能体不得超期后补发。

    外部实现走的是 `AgentKit` 这层平台自己的契约壳：壳有"内软超时"（比网关的 deadline 早 50ms），
    所以超期时平台先收到的是一封 `E_TIMEOUT` 的错误回执——**记的还是 E_TIMEOUT**，
    事务也照样不计成功（只有"成功回执"才记 ok）。这是与 Mock 智能体（无内软超时、由网关判超时）
    的口径差别，不是外部实现的问题。
    """

    async def test_迟到回执按超时记账_事务不计成功(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        ext_runner.delay_s = 0.4
        with pytest.raises(AegisError):
            await gateway.dispatch(
                AgentType.ASSESS,
                Action.ASSESS_HAZARD,
                _assess_payload(),
                trace_id=TRACE,
                capability="risk_assess",
                deadline_ms=200,
            )
        assert gateway.transactions[-1].outcome != "ok", "超期的事务不许记成成功"
        assert gateway.transactions[-1].error_code == "E_TIMEOUT"
        assert gateway.success_rate() == 0.0
        await agent.stop()
        await gateway.close()

    async def test_超期后不再补发成功回执也不二次执行(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        """超期之后不得再补一封"成功"回执——那会让平台把一条过期结论当成新结论采纳。"""
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        ext_runner.delay_s = 0.4
        request = make_request(
            source="platform.gateway",
            target=agent.agent_id,
            action=Action.ASSESS_HAZARD.value,
            payload=_assess_payload(),
            trace_id=TRACE,
            reply_to="reply.gwext0002.inbox",
            deadline_ms=200,
        )
        receipts = await collect_receipts(bus, request)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)
        await wait_for_receipts(bus, receipts)  # 内软超时（deadline - 50ms）到点即回执

        assert len(receipts) == 1, "超期只允许一封回执"
        assert receipts[0].kind is MessageKind.ERROR, "超期必须走 error 通道，不许回一封正常 response"
        assert receipts[0].payload["code"] == "E_TIMEOUT"
        assert receipts[0].payload["retryable"] is True

        await asyncio.sleep(0.5)  # 等她那一腿跑完（她的图跑不掉：ADR-0011 记的已知限制）
        await bus.idle()
        assert len(receipts) == 1, "超期后不得再补一封成功回执"
        assert agent.handled == 1, "超期不得触发二次执行"
        await agent.stop()
        await gateway.close()


class TestGate4ErrorReply:
    """门禁 4：内部异常必须回 `error.raise` 而非静默；未注册动作的上行必须被网关拒收。"""

    async def test_内部异常收敛成类型化错误回执(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        ext_runner.result = RuntimeError("她的内部炸了")
        with pytest.raises(AegisError) as info:
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, _assess_payload(), trace_id=TRACE, capability="risk_assess")
        assert info.value.retryable is True, "内部异常是可重试的：平台据此走重试/降级轨"
        assert gateway.transactions[-1].outcome == "error"
        assert gateway.transactions[-1].error_code == "E_EXT_FAILED"
        await agent.stop()
        await gateway.close()

    async def test_外部失败必须走error通道_不得是正常response(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        """失败塞进 response 的嵌套 dict 会被网关**当成功记账**——协同成功率虚高（评审抓过的真问题）。

        所以这条钉的是消息的 `kind`：必须是 `error`，且回执里不得夹带走定级的字段。
        """
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        ext_runner.result = RuntimeError("她的内部炸了")
        request = make_request(
            source="platform.gateway",
            target=agent.agent_id,
            action=Action.ASSESS_HAZARD.value,
            payload=_assess_payload(),
            trace_id=TRACE,
            reply_to="reply.gwext0003.inbox",
            deadline_ms=800,
        )
        receipts = await collect_receipts(bus, request)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)
        await bus.idle()
        await wait_for_receipts(bus, receipts)
        await settle(bus)

        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt.kind is MessageKind.ERROR, "失败必须走 error 通道"
        assert receipt.kind is not MessageKind.RESPONSE
        assert receipt.payload["code"] == "E_EXT_FAILED"
        assert receipt.payload["retryable"] is True
        assert "risk_level" not in receipt.payload, "失败的回执里不得夹带走定级的结论"

        with pytest.raises(AegisError):
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, _assess_payload(), trace_id=TRACE, capability="risk_assess")
        assert gateway.transactions[-1].outcome == "error", "网关必须记 error，不能记成功"
        assert gateway.success_rate() == 0.0
        await agent.stop()
        await gateway.close()

    async def test_未注册动作的上行被网关拒收(self, bus: InMemoryBus, gateway: AgentGateway) -> None:
        """外部实现不得把未注册的动作塞上总线：网关按契约拒收并记账。"""
        await gateway.start()
        await bus.publish(
            subjects.agent_out(AgentType.ASSESS),
            AgentMessage(
                source="assess.ext01",
                target="platform.sink",
                kind=MessageKind.EVENT,
                action="assess.teleport",
                payload={},
                trace_id=TRACE,
            ),
        )
        await bus.idle()
        assert gateway.counters["rejected"] == 1
        await gateway.close()


class TestGate5Lifecycle:
    """门禁 5：心跳与注册——注册即在册，断心跳被摘除，恢复后重新可用。"""

    async def test_注册即在册且能力声明正确(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        entry = registry.get(agent.agent_id)
        assert entry is not None, "外部实现注册后必须立刻在册"
        assert entry.healthy
        assert entry.reg.capabilities == ["risk_assess"]
        assert entry.reg.agent_type == AgentType.ASSESS.value
        assert registry.pick(AgentType.ASSESS.value, "risk_assess") is not None
        await agent.stop()
        await gateway.close()

    async def test_断心跳后摘除路由_恢复后重新可用(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        await agent.stop()  # 心跳停止
        entry = registry.get(agent.agent_id)
        assert entry is not None
        entry.last_hb_mono = 0.0  # 把心跳时间戳推到远古：模拟"连续丢失心跳"
        assert await registry.sweep_and_notify() == [agent.agent_id]
        assert registry.pick(AgentType.ASSESS.value, "risk_assess") is None, "失联的本体不得再被派单"

        await agent.start(heartbeat_interval=0.05)  # 重新注册
        await bus.idle()
        assert registry.pick(AgentType.ASSESS.value, "risk_assess") is not None
        await agent.stop()
        await gateway.close()


class TestGate6DataBoundary:
    """门禁 6：数据边界——外部实现只能经总线与平台数据面取数，不得直连数据库。"""

    def test_外部实现不直连存储层(self) -> None:
        """静态检查（平台门禁同一组禁令）：外部模块里不得出现存储层/数据库驱动。"""
        forbidden = {"sqlalchemy", "aegis.storage", "neo4j", "asyncpg", "redis", "minio"}
        offenders: list[str] = []
        for path in EXT_PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                offenders += [f"{path.name}:{name}" for name in names if name in forbidden or name.startswith("aegis.storage")]
        assert offenders == [], f"外部实现不得直接访问平台存储层: {offenders}"

    def test_适配层实例上没有数据库客户端(self, bus: InMemoryBus, ext_runner: FakeExtRunner) -> None:
        """运行时探测：适配层本体上不得挂着数据库连接/会话这类属性。"""
        adapter = ExternalAgentAdapter(
            bus,
            agent_type=AgentType.ASSESS,
            agent_id="assess.ext01",
            capabilities=["risk_assess"],
            action="assess.hazard",
        )
        fields = {name.lower() for name in vars(adapter)}
        assert not (fields & {"session", "engine", "connection", "repository", "store", "db"})

    async def test_取数只经平台下发的观测_不自己连库(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        """外部实现的输入只来自平台下发的载荷：观测走 `data_result`、命中走 `hits` 骨架。"""
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        await gateway.dispatch(
            AgentType.ASSESS,
            Action.ASSESS_HAZARD,
            _assess_payload(),
            trace_id=TRACE,
            capability="risk_assess",
        )
        assert ext_runner.calls, "外部实现必须被真正调用过（否则这条断言是空的）"
        call = ext_runner.calls[-1]
        assert call["data_result"]["processed_data"]["data_source"] == "platform_hits", "数据只能来自平台下发的载荷"
        assert call["data_result"]["processed_data"]["events"][0]["event_id"] == "R-DEBRIS-RAIN-1"
        await agent.stop()
        await gateway.close()


class TestGate7ContextSnapshot:
    """门禁 7：上下文快照能被恢复。

    **本项在外部实现上覆盖不到完整形态**：STU 是决策段（`plan.stu`）的产出，
    而外部实现目前只接线了 `assess.hazard`，决策段未接线 ⇒ 没有 STU ⇒
    `context_snapshot` 这一层无从产生。钉它就得先接线，未接线不钉（不拿"没接线"当"过了"）。

    这里能诚实覆盖的是**上游那一半**：研判结论必须带齐"快照要恢复所需"的字段
    （灾种/区域/等级/置信度/依据/证据/署名/数据出处），否则下游即使接上了决策段也恢复不出快照。
    另外钉住"未接线的决策段不伪造快照"。
    """

    async def test_研判结论带齐可恢复快照所需字段(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        reply = await gateway.dispatch(
            AgentType.ASSESS,
            Action.ASSESS_HAZARD,
            _assess_payload(),
            trace_id=TRACE,
            capability="risk_assess",
        )
        payload = reply.payload
        for field in ("hazard_type", "region_code", "risk_level", "confidence", "rationale", "evidence_refs", "assessed_by"):
            assert field in payload, f"快照要恢复的字段缺了：{field}"
        assert payload["hazard_type"] in [item.value for item in HazardType]
        assert int(payload["risk_level"]) in [int(level) for level in RiskLevel]
        assert 0.0 <= float(payload["confidence"]) <= 1.0
        assert payload["rationale"], "快照要能恢复出「依据」，不能只剩一个等级"
        # 快照要能被存下来再读回来：结论必须是可序列化的纯数据
        assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload
        await agent.stop()
        await gateway.close()

    async def test_未接线的决策段不伪造快照(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        """决策段未接线时，平台拿到的是类型化错误，而不是一份没有 `context_snapshot` 的 STU。

        没接线就产不出来的快照，比产出一份假快照安全：后者会被下游当成"可恢复的现场"接着跑。
        """
        agent = await bring_up_ext(bus, gateway, AgentType.PLAN)
        with pytest.raises(AegisError) as info:
            await gateway.dispatch(
                AgentType.PLAN,
                Action.PLAN_STU,
                {"event_id": "evt_" + "c" * 12, "risk": {}},
                trace_id=TRACE,
                capability="task_decompose",
            )
        assert info.value.detail["code"] == CODE_ACTION_UNSUPPORTED
        assert gateway.transactions[-1].outcome == "error"
        await agent.stop()
        await gateway.close()


class Test边界口径:
    """外部接入最容易出事的四条口径（评审点名的重点）。

    平台与外部实现对同一件事有两套词汇（灾种键、等级档位、状态四态、署名），
    对不上就出现"结论 100% 被丢弃"或"拿不到就说没有"。
    """

    @pytest.mark.parametrize(
        "platform_key,ext_key",
        [("avalanche", "ice_rock_snow_avalanche"), ("lake_outburst", "glacial_lake_outburst")],
    )
    async def test_灾种键两套名字双向映射(
        self,
        bus: InMemoryBus,
        gateway: AgentGateway,
        registry: AgentRegistry,
        ext_runner: FakeExtRunner,
        platform_key: str,
        ext_key: str,
    ) -> None:
        """平台 `avalanche` ↔ 外部 `ice_rock_snow_avalanche`（冰湖同理）。

        没有这张表，这两个灾种的外部结论 100% 被丢弃（`HazardType(...)` 抛 ValueError），
        而且看不出是哪里不对。
        """
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        ext_runner.result = _risk_result(hazard_type=ext_key)
        reply = await gateway.dispatch(
            AgentType.ASSESS,
            Action.ASSESS_HAZARD,
            _assess_payload(hazards=[platform_key]),
            trace_id=TRACE,
            capability="risk_assess",
        )
        assert ext_runner.calls[-1]["hazard_type"] == ext_key, "平台键必须翻译成她认识的键再发过去"
        assert reply.payload["hazard_type"] == platform_key, "她的键必须翻译回平台键再上行"
        await agent.stop()
        await gateway.close()

    async def test_平台独有灾种外部不支持时明确拒绝不硬塞(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        """`quake_triggered` 外部不支持：必须明确拒绝，且不得硬塞一个近似灾种。

        **已知生产缺陷（本文件不改生产代码，如实记一笔）**：她这条错误的错误码
        `E_EXT_HAZARD_UNSUPPORTED` 有 24 个字符，超过 `agent_message.v1` 的
        `^E[A-Z0-9_]{3,20}$`（上限 21 字符）——网关按契约把这封错误回执当
        `E_SCHEMA_INVALID` 拒收，平台侧因此看不到"外部实现不支持该灾种"这个原因，
        连事务台账里都不留这一笔（`validate` 在记账之前抛）。
        所以这里只断言**两种世界都成立**的那几条（不硬塞近似灾种 / 不产出结论 /
        不记成协同成功）；错误码本身要在生产代码把码收短（如 `E_EXT_HAZARD_UNSUP`）
        之后才补断言 `detail["code"] == "E_EXT_HAZARD_UNSUPPORTED"`。
        """
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        with pytest.raises(AegisError):
            await gateway.dispatch(
                AgentType.ASSESS,
                Action.ASSESS_HAZARD,
                _assess_payload(hazards=["quake_triggered"]),
                trace_id=TRACE,
                capability="risk_assess",
            )
        assert ext_runner.calls == [], "灾种不支持时不得替她挑一个近似灾种去跑"
        assert all(record.outcome != "ok" for record in gateway.transactions), "被拒的灾种不许记成协同成功"
        await agent.stop()
        await gateway.close()

    async def test_外部档位认不出时不产定级(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        """她的档位认不出时**不给定级**（不是"无风险"）。

        平台的 5 是"无风险"，把"算不出来"写成 5 就是"拿不到就说没有"——
        所以这里断言的是：平台没收到任何定级，而不是收到了 5。
        """
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        ext_runner.result = _risk_result(risk_level="unknown")
        request = make_request(
            source="platform.gateway",
            target=agent.agent_id,
            action=Action.ASSESS_HAZARD.value,
            payload=_assess_payload(),
            trace_id=TRACE,
            reply_to="reply.gwext0004.inbox",
            deadline_ms=800,
        )
        receipts = await collect_receipts(bus, request)
        await bus.publish(subjects.agent_in(AgentType.ASSESS), request)
        await bus.idle()
        await wait_for_receipts(bus, receipts)
        await settle(bus)

        assert len(receipts) == 1
        assert receipts[0].kind is MessageKind.ERROR
        assert receipts[0].payload["code"] == "E_EXT_LEVEL_UNKNOWN"
        assert "risk_level" not in receipts[0].payload, "认不出的档位不得产出定级（含不得落成 5=无风险）"

        with pytest.raises(AegisError):
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, _assess_payload(), trace_id=TRACE, capability="risk_assess")
        assert gateway.transactions[-1].outcome == "error", "没定级的事务不许记成成功"
        await agent.stop()
        await gateway.close()

    async def test_数据不足时不给定级(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        """她的四态里只有 success 产出定级；数据不足要带上原因，别只给一个错误码。"""
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        ext_runner.result = _risk_result(status="insufficient_data", missing_info=["rainfall_mm_h"], risk_level="unknown")
        with pytest.raises(AegisError) as info:
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, _assess_payload(), trace_id=TRACE, capability="risk_assess")
        assert info.value.detail["code"] == "E_EXT_INSUFFICIENT"
        assert "rainfall_mm_h" in info.value.message, "原因要带她的原话，读的人才知道缺什么"
        assert gateway.transactions[-1].outcome == "error"
        await agent.stop()
        await gateway.close()

    async def test_她的结果缺置信度时不得产出定级(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        """**已知缺口**：她的 `risk_result` 里没有 `confidence`（因子引擎给的是 provisional_level）。

        契约要求 confidence，平台要校验 0..1；拿不到真值就不产结论——
        填 0.0 会被平台当成一条有效研判采纳，那比不给更坏。
        """
        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        her_result = _risk_result()
        her_result.pop("confidence")
        ext_runner.result = her_result
        with pytest.raises(AegisError) as info:
            await gateway.dispatch(AgentType.ASSESS, Action.ASSESS_HAZARD, _assess_payload(), trace_id=TRACE, capability="risk_assess")
        assert info.value.detail["code"] == "E_EXT_NO_CONFIDENCE"
        assert gateway.transactions[-1].outcome == "error"
        await agent.stop()
        await gateway.close()

    async def test_外部结论的署名与平台自建内环不同(
        self, bus: InMemoryBus, gateway: AgentGateway, registry: AgentRegistry, ext_runner: FakeExtRunner
    ) -> None:
        """台账要能分清"这条结论是谁答的"。

        平台自建内环的署名是它的**实例 id**（见 `aegis/agents/assess.py`），
        外部实现必须用另一个署名——否则两套结论混在一起，事后没法归因。
        """
        from aegis.agents.factory import build_real_agents

        agent = await bring_up_ext(bus, gateway, AgentType.ASSESS)
        reply = await gateway.dispatch(
            AgentType.ASSESS,
            Action.ASSESS_HAZARD,
            _assess_payload(),
            trace_id=TRACE,
            capability="risk_assess",
        )
        builtin_ids = {item.agent_id for item in build_real_agents(bus)}
        assessed_by = reply.payload["assessed_by"]
        assert assessed_by, "结论必须有署名"
        assert assessed_by not in builtin_ids, "外部实现不能与平台自建内环共用署名"
        # 署名用**实例 id**（与自建内环同一口径，见 agents/assess.py 的 `self.agent_id`）：
        # 既能与自建内环区分开，又能分清是哪一台外部本体答的（只写个实现名做不到后者）。
        assert assessed_by == agent.agent_id
        assert reply.payload["source"] == SOURCE_MARK, "数据出处也要能分清"
        await agent.stop()
        await gateway.close()


class Test接线真实性:
    """替身不许测一个虚构接口：外部实现的入口签名要与适配层的调用口径一致。"""

    def test_适配层调用的入口签名与真实实现一致(self) -> None:
        pytest.importorskip("langgraph", reason="未安装可选依赖 agents_ext（`uv sync --extra agents_ext`）")
        from aegis.agents_ext.risk_agent.main import run_risk_agent

        params = inspect.signature(run_risk_agent).parameters
        for name in ("task", "data_result", "hazard_type"):
            assert name in params, f"适配层按关键字调用 {name}，真实入口却没有这个参数"
        assert inspect.signature(FakeExtRunner.__call__).parameters.keys() >= {"task", "data_result", "hazard_type"}

    def test_适配层的动作分派只认它注册的那一个动作(self) -> None:
        """接线表来自 `build_ext_agents`：每个本体只处理一个动作，其余按契约拒绝。"""
        agents = build_ext_agents(InMemoryBus())
        wired = {item.agent_type.value: item._action for item in agents}
        assert wired == {
            "perceive": "assess.hazard",
            "assess": "assess.hazard",
            "plan": "plan.stu",
            "execute": "execute.warn",
            "feedback": "feedback.attribute",
        }
