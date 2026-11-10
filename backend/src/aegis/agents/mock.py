"""参考/占位智能体：严格按契约实现的 Mock 智能体集。

三重用途（对应《智能体接入规范》§7）：
1. 平台侧独立开发与联调——不等智能体方进度；
2. 端到端演示与压测的基线载荷；
3. 一致性测试的合格样例，以及可注入违约行为（超时/非法载荷/静默/异常）的不合格样例。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import zlib
from dataclasses import dataclass, field
from typing import Any, ClassVar

from aegis.agents.capabilities import capabilities_for
from aegis.agents.hazards.contracts import (
    data_quality_response,
    factor_risk_response,
    warn_content_response,
)
from aegis.bus import subjects
from aegis.bus.naming import consumer_name
from aegis.bus.transport import BusTransport
from aegis.domain.enums import Action, AgentType, HazardType, MessageKind, RiskLevel
from aegis.domain.messages import (
    AgentMessage,
    CapabilityRegistration,
    TriggerHit,
    make_error,
    make_response,
)
from aegis.domain.provenance import DataProvenance
from aegis.domain.verdict import RiskVerdict
from aegis.services.response.task_parser import TaskParser

log = logging.getLogger("aegis.agents.mock")

_EVENT_ID_RE = re.compile(r"^evt_[0-9a-f]{12}$")


class Misbehavior:
    NONE = "none"
    LATE = "late"  # 超过 deadline 才回复 → 网关应判 E_TIMEOUT
    SILENT = "silent"  # 完全不回复
    INVALID = "invalid"  # 回复载荷不合契约
    CRASH = "crash"  # 回 error.raise


@dataclass
class MockAgent:
    """单个智能体实例：注册 → 心跳 → 消费 in → 产出 out。"""

    transport: BusTransport
    agent_type: AgentType
    agent_id: str
    capabilities: list[str]
    hazard_types: list[str] = field(default_factory=lambda: [h.value for h in HazardType if h is not HazardType.UNKNOWN])
    latency_ms: float = 30.0
    fail_rate: float = 0.0
    misbehavior: str = Misbehavior.NONE
    max_concurrency: int = 4
    seed: int | None = None
    version: str = "mock-1.0.0"
    handled: int = 0
    duplicates_skipped: int = 0
    _seen_ids: dict[str, None] = field(default_factory=dict, repr=False)
    _dedup_capacity: int = 4_096
    _subs: list[Any] = field(default_factory=list)
    _hb_task: asyncio.Task[None] | None = None
    _rng: random.Random = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    @property
    def registration(self) -> CapabilityRegistration:
        return CapabilityRegistration(
            agent_id=self.agent_id,
            agent_type=self.agent_type.value,
            capabilities=self.capabilities,
            hazard_types=self.hazard_types,
            max_concurrency=self.max_concurrency,
            version=self.version,
        )

    async def start(self, *, heartbeat_interval: float = 1.0) -> None:
        # 与 AgentKit 同序：先建收件箱再宣告上线，否则注册后立刻派来的第一条请求会落空。
        # 专属口同样 durable：网关默认往这里派单，核心订阅断线即丢。
        self._subs.append(
            await self.transport.subscribe(
                subjects.agent_in(self.agent_type, self.agent_id),
                self._on_request,
                queue=consumer_name("cg_in", self.agent_type.value, self.agent_id),
                durable=consumer_name("d_in", self.agent_type.value, self.agent_id),
            )
        )
        self._subs.append(
            await self.transport.subscribe(
                subjects.agent_in(self.agent_type),
                self._on_request,
                # 名字里带 agent_id，而 agent_id 可以有点（`perceive.mock01`）：
                # JetStream 的消费者名校验会直接拒掉，内存总线却不报错——见 bus/naming.py。
                queue=consumer_name("cg", self.agent_type.value, self.agent_id),
                durable=consumer_name("d", self.agent_type.value, self.agent_id),
            )
        )
        await self.transport.publish(subjects.agent_hb(self.agent_type), self._frame(Action.AGENT_REGISTER))
        self._hb_task = asyncio.create_task(self._heartbeat_loop(heartbeat_interval), name=f"hb-{self.agent_id}")

    async def stop(self) -> None:
        if self._hb_task is not None:
            self._hb_task.cancel()
            await asyncio.gather(self._hb_task, return_exceptions=True)
            self._hb_task = None
        for sub in self._subs:
            await sub.cancel()
        self._subs.clear()

    def _frame(self, action: Action) -> AgentMessage:
        return AgentMessage(
            source=self.agent_id,
            target="platform.registry",
            kind=MessageKind.EVENT,
            action=action.value,
            payload={"agent_id": self.agent_id, "registration": self.registration.model_dump()},
        )

    async def _heartbeat_loop(self, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await self.transport.publish(subjects.agent_hb(self.agent_type), self._frame(Action.AGENT_HEARTBEAT))
        except asyncio.CancelledError:
            return

    async def _on_request(self, request: AgentMessage) -> None:
        # 公共收件口是广播：只处理点名的请求，替身也不能例外，否则"N 个替身都执行一遍"会被测试掩盖。
        if request.target not in ("*", self.agent_id):
            return
        # 接收方按 msg_id 幂等去重（规范 §1）：重复投递不得产生二次副作用
        if request.msg_id in self._seen_ids:
            self.duplicates_skipped += 1
            return
        self._seen_ids[request.msg_id] = None
        while len(self._seen_ids) > self._dedup_capacity:
            self._seen_ids.pop(next(iter(self._seen_ids)), None)

        self.handled += 1
        reply_subject = request.reply_to or subjects.agent_out(self.agent_type)

        if self.misbehavior == Misbehavior.SILENT:
            return
        if self.misbehavior == Misbehavior.CRASH:
            await self._publish(
                reply_subject,
                make_error(
                    request,
                    source=self.agent_id,
                    code="E_INTERNAL",
                    message="mock 智能体内部异常",
                    retryable=True,
                ),
            )
            return

        await asyncio.sleep(self.latency_ms / 1000)
        if self._rng.random() < self.fail_rate:
            await self._publish(
                reply_subject,
                make_error(
                    request,
                    source=self.agent_id,
                    code="E_INTERNAL",
                    message="mock 随机失败",
                    retryable=True,
                ),
            )
            return

        if self.misbehavior == Misbehavior.LATE:
            await asyncio.sleep(max(request.deadline_ms or 1_000, 1_000) * 1.5 / 1000)

        payload = {"unexpected": "shape"} if self.misbehavior == Misbehavior.INVALID else self._respond(request)
        await self._publish(reply_subject, make_response(request, source=self.agent_id, payload=payload))

    async def _publish(self, subject: str, message: AgentMessage) -> None:
        await self.transport.publish(subject, message)

    def _respond(self, request: AgentMessage) -> dict[str, Any]:
        """按 `(agent_type, action)` 分派。

        同一个阶段下会有多项能力（研判段有主结论、证据门、因子交叉验证、情景仿真四条），
        只按 agent_type 分派分不出来——请求里带着动作，动作才是"这次要哪条腿"的判据。
        """
        handler = self._HANDLERS.get((self.agent_type, request.action))
        if handler is not None:
            return handler(self, request)
        return {"status": "ok", "warning_id": request.payload.get("warning_id", "")}

    def _perceive(self, request: AgentMessage) -> dict[str, Any]:
        """与 PerceiveAgent 的应答同形：感知段回收的是 trigger_hits，空的含义是"没有新增命中"。"""
        return {"anomaly_count": 0, "trigger_hits": []}

    def _assess(self, request: AgentMessage) -> dict[str, Any]:
        hits = request.payload.get("hits", [])
        first = hits[0] if isinstance(hits, list) and hits else {}
        level = RiskLevel.RED if isinstance(hits, list) and len(hits) > 1 else RiskLevel.ORANGE
        region = str(request.payload.get("region_code") or first.get("region_code") or "540100A1")
        return {
            "contract_version": "assess.v1",
            "hazard_type": str(first.get("hazard_type", HazardType.DEBRIS_FLOW.value)),
            "region_code": region,
            "risk_level": int(level),
            "confidence": 0.82,
            "confidence_basis": "mock_agent:0.82",
            "rationale": f"mock 研判：{len(hits)} 条触发命中",
            "assessed_by": self.agent_id,
            "source": "platform.mock",
            "data_provenance": DataProvenance(
                data_kind="mock",
                sources=["mock_agent"],
                method="mock_agent",
                is_mock=True,
                disclaimer="替身智能体产物，仅用于联调。",
            ).model_dump(),
        }

    def _plan(self, request: AgentMessage) -> dict[str, Any]:
        risk = request.payload.get("risk") or {}
        if not isinstance(risk, dict):
            return {"task_units": []}
        try:
            hazard = HazardType(str(risk.get("hazard_type", HazardType.DEBRIS_FLOW.value)))
            region = str(risk.get("region_code", "540100A1"))
            level = RiskLevel(int(risk.get("risk_level", int(RiskLevel.ORANGE))))
            confidence = float(risk.get("confidence", 0.7))
        except (ValueError, TypeError):
            return {"task_units": []}

        evidence = [e for e in risk.get("evidence_refs", []) if isinstance(e, str)]
        hits = [
            TriggerHit(
                rule_id=item if item.startswith("R-") else f"R-MOCK-{index}",
                hazard_type=hazard.value,
                region_code=region,
                score=0.7,
            )
            for index, item in enumerate(evidence)
        ] or [TriggerHit(rule_id="R-MOCK-DEFAULT", hazard_type=hazard.value, region_code=region, score=0.6)]
        verdict = RiskVerdict(
            hazard_type=hazard,
            region_code=region,
            risk_level=level,
            confidence=confidence,
            rationale=str(risk.get("rationale", "mock 拆解"))[:500],
            hits=hits,
            data_provenance=DataProvenance.from_payload(risk.get("data_provenance")),
            confidence_basis=str(risk.get("confidence_basis", "")),
        )
        raw_event = str(request.payload.get("event_id", ""))
        event_id = raw_event if _EVENT_ID_RE.match(raw_event) else None
        units = TaskParser().parse(verdict, event_id=event_id)
        return {"task_units": [{**unit.model_dump(exclude_none=True), "created_by": self.agent_id} for unit in units]}

    def _execute(self, request: AgentMessage) -> dict[str, Any]:
        channels = request.payload.get("channels", [])
        audience_count = len(request.payload.get("audiences", []))
        return {
            "warning_id": str(request.payload.get("warning_id", "")),
            "channel_results": [
                {
                    "channel": str(channel),
                    "audience_count": audience_count,
                    "status": "delivered",
                    "provider_msg_id": f"mock-{self.agent_id}-{channel}",
                }
                for channel in channels
            ],
        }

    # ---------- 灾种链的六条腿 ----------

    def _data_quality(self, request: AgentMessage) -> dict[str, Any]:
        """证据质量门：替身给一份"合格"的判据，让研判段的下游路径能被完整走到。"""
        payload = request.payload or {}
        observations = (payload.get("evidence") or {}).get("observations") or []
        total = len(observations)
        return data_quality_response(
            {
                "is_valid": True,
                "completeness_ratio": 1.0 if total else 0.0,
                "validation_reason": "mock 替身：证据质量合格",
                "sufficient": bool(total) or not payload.get("evidence"),
                "reason": "mock 替身：证据质量合格",
                "missing_data": [],
            }
        )

    def _factor_risk(self, request: AgentMessage) -> dict[str, Any]:
        """因子引擎交叉验证：与主研判**同档**——分歧腿的价值在于比对，不在于制造分歧。"""
        payload = request.payload or {}
        hits = payload.get("hits") or []
        first = hits[0] if isinstance(hits, list) and hits else {}
        level = RiskLevel.RED if isinstance(hits, list) and len(hits) > 1 else RiskLevel.ORANGE
        region = str(payload.get("region_code") or first.get("region_code") or "540100A1")
        return factor_risk_response(
            {
                "hazard_type": str(first.get("hazard_type", HazardType.DEBRIS_FLOW.value)),
                "region_code": region,
                "risk_level": int(level),
                "confidence": 0.55,
                "confidence_basis": "mock_agent:0.55",
                "rationale": f"mock 因子引擎：{len(hits)} 条触发命中",
                "evidence_refs": [],
                "source": "platform.mock",
                "assessed_by": self.agent_id,
                "human_review_suggested": True,
                "assessment": {"engine_version": "MOCK-FACTOR-v0.1"},
                "data_provenance": {"data_kind": "mock", "sources": ["mock_agent"], "method": "mock", "is_mock": True},
            }
        )

    def _scenario_sim(self, request: AgentMessage) -> dict[str, Any]:
        """情景仿真：产出全程带 mock 标记——替身不装成真模型。"""
        payload = request.payload or {}
        return {
            "hazard_type": str(payload.get("hazard_type") or ""),
            "status": "success",
            "simulation_results": [],
            "comparison": {"comparison_table": [], "ranking": [], "key_findings": []},
            "missing_inputs": [],
            "unsupported_reason": "",
            "model_info": {"is_real_prediction": False, "model_version": "MOCK-v0.1"},
            "is_mock": True,
            "is_real_prediction": False,
        }

    def _hazard_workflow(self, request: AgentMessage) -> dict[str, Any]:
        """灾种闭环：替身把五步都标成已提供，产出空报告。"""
        payload = request.payload or {}
        return {
            "event_id": str(payload.get("event_id") or ""),
            "hazard_type": str(payload.get("hazard_type") or ""),
            "region": str(payload.get("region") or ""),
            "task_status": "completed",
            "agent_status": {
                "data_agent": "provided",
                "risk_agent": "provided",
                "simulation_agent": "skipped",
                "warning_agent": "provided",
                "feedback_agent": "provided",
            },
            "execution_plan": ["data_agent", "risk_agent", "simulation_agent", "warning_agent", "feedback_agent"],
            "final_report": {"summary": "mock 闭环报告", "mock_only": True},
            "error_history": [],
        }

    def _warn_content(self, request: AgentMessage) -> dict[str, Any]:
        """预警素材：只有素材，没有发布动作。"""
        payload = request.payload or {}
        return warn_content_response(
            {
                "explanation_text": "mock 预警文案",
                "suggested_actions": [],
                "cap_xml": "",
                "cap_dict": {},
                "based_on": [],
                "warning_level": str(payload.get("level") or ""),
                "target_region": ",".join(payload.get("regions") or []),
                "is_mock": True,
            }
        )

    def _chain_audit(self, request: AgentMessage) -> dict[str, Any]:
        """链内审计：没有发现不一致。"""
        payload = request.payload or {}
        return {
            "hazard_type": str(payload.get("hazard_type") or ""),
            "status": "success",
            "feedback_id": "",
            "event_id": str(payload.get("event_id") or ""),
            "summary": {"issue_count": 0, "actionable_issue_count": 0, "needs_human_review_count": 0},
            "issue_count": 0,
            "actionable_issue_count": 0,
            "needs_human_review_count": 0,
            "feedback_items": [],
            "improvement_suggestions": [],
            "manager_actions": [],
            "reflection_result": {},
            "safety_declarations": {},
        }

    def _feedback(self, request: AgentMessage) -> dict[str, Any]:
        """触达归因：字段与平台 `feedback.attribute` 的应答要求对齐。"""
        payload = request.payload or {}
        reach = payload.get("reach_stats") or {}
        total = int(reach.get("total") or 0)
        delivered = int(reach.get("delivered") or 0)
        effect = round(delivered / total, 4) if total else 0.0
        return {
            "warning_id": str(payload.get("warning_id") or ""),
            "effect_score": effect,
            "attribution": f"mock 归因：触达 {delivered}/{total}",
            "unreached_audiences": [],
            "lag_stage": "",
            "threshold_suggestion": "",
            "suggestion_status": "pending_human_review",
            "decided_by": "mock",
        }

    #: `(agent_type, action)` → 应答函数。动作才是"这次要哪条腿"的判据：同一个阶段下
    #: 会有多项能力（研判段四条、决策段两条、执行段两条、反馈段两条），只按类型分派
    #: 分不出来。
    _HANDLERS: ClassVar[dict[tuple[AgentType, str], Any]] = {
        (AgentType.PERCEIVE, Action.ASSESS_HAZARD.value): _perceive,
        (AgentType.ASSESS, Action.ASSESS_HAZARD.value): _assess,
        (AgentType.ASSESS, Action.ASSESS_DATA_QUALITY.value): _data_quality,
        (AgentType.ASSESS, Action.ASSESS_FACTOR_RISK.value): _factor_risk,
        (AgentType.ASSESS, Action.ASSESS_SCENARIO_SIM.value): _scenario_sim,
        (AgentType.PLAN, Action.PLAN_STU.value): _plan,
        (AgentType.PLAN, Action.PLAN_HAZARD_WORKFLOW.value): _hazard_workflow,
        (AgentType.EXECUTE, Action.EXECUTE_WARN.value): _execute,
        (AgentType.EXECUTE, Action.EXECUTE_WARN_CONTENT.value): _warn_content,
        (AgentType.FEEDBACK, Action.FEEDBACK_ATTRIBUTE.value): _feedback,
        (AgentType.FEEDBACK, Action.FEEDBACK_CHAIN_AUDIT.value): _chain_audit,
    }


async def publish_response(transport: BusTransport, message: AgentMessage, reply_to: str) -> None:
    await transport.publish(reply_to, message)


async def start_mock_agents(
    transport: BusTransport,
    *,
    latency_ms: float = 20.0,
    fail_rate: float = 0.0,
    misbehavior: dict[AgentType, str] | None = None,
    heartbeat_interval: float = 0.5,
) -> list[MockAgent]:
    """拉起五类智能体的 Mock 实例。注意：响应回投递需 request.reply_to，见 MockAgent._on_request。"""
    misbehavior = misbehavior or {}
    # 能力表取自能力矩阵：替身要覆盖矩阵里该阶段的**全部**能力，否则矩阵里的某项能力
    # 在 mock 档就没人接——而 mock 档正是 CI 与演练跑得最多的形态。
    specs = [(agent_type, list(capabilities_for(agent_type))) for agent_type in AgentType]
    agents = [
        MockAgent(
            transport=transport,
            agent_type=agent_type,
            agent_id=f"{agent_type.value}.mock01",
            capabilities=caps,
            latency_ms=latency_ms,
            fail_rate=fail_rate,
            misbehavior=misbehavior.get(agent_type, Misbehavior.NONE),
            # 种子必须与进程无关：`hash()` 受 PYTHONHASHSEED 支配，同一个提交在两台机器上会给出
            # 不同的延迟分布——演练数字就成了机器巧合。crc32 是稳定映射。
            seed=zlib.crc32(agent_type.value.encode()) % 65_536,
        )
        for agent_type, caps in specs
    ]
    for agent in agents:
        await agent.start(heartbeat_interval=heartbeat_interval)
    return agents
