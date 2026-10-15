"""assess 研判智能体内环（§3.2）：LLM 结构化定级 + 双盲交叉验证 + D10 溯源。

R1/R8/R11 的落点：定级由 `chat_structured`（1–5 枚举强约束，越界即拒）产出，红/橙级触发
双盲（第二评估 + 分歧仲裁），结论自带 `model_info`（可复现）与 `disagreement`。取数经注入的
`AssessContextProvider`（Protocol），**不碰存储层**；LLM 缺位/超时/不合规一律让内环异常上抛，
由 agentkit 回执 error、平台规则降级轨接管（LLM 不可用≠平台不可用）。
上抛只适用于**关键路径**（第一遍定级）：双盲是增强腿，它的失败在腿内收口成 `double_blind=failed_*`
并保留第一遍结论——一次模型故障不该废掉已经拿到的研判。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from aegis.agents.agentkit import AgentKit, Reply
from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import AgentMessage, ModelInfo, now_iso
from aegis.services.memory import MemoryService, new_memory_record_id
from aegis.services.prompt_guard import fence_untrusted

log = logging.getLogger("aegis.agents.assess")


class AssessVerdict(BaseModel):
    """LLM 结构化定级的目标 schema：1–5 枚举冻结、置信度 0..1、rationale/evidence 必填可溯源。"""

    model_config = {"extra": "forbid"}

    risk_level: int = Field(ge=1, le=5)
    #: 灾种必须逐字落在 `HazardType` 上。此前这格是无约束 str：模型交回
    #: "暴雨诱发山洪、泥石流及滑坡" 也算通过，于是下游 `HazardType(...)` 要么炸、要么静默变 unknown
    #: ——那正是"定级一致率/灾种对不对"最容易被抹平的地方。
    hazard_type: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("risk_level")
    @classmethod
    def _frozen(cls, v: int) -> int:
        if v not in tuple(int(x) for x in RiskLevel):
            raise ValueError("等级必须落在 1-5 冻结枚举内")
        return v

    @field_validator("hazard_type")
    @classmethod
    def _hazard_in_enum(cls, v: str) -> str:
        allowed = tuple(str(item.value) for item in HazardType)
        if v not in allowed:
            raise ValueError(f"未知 hazard_type: {v}（只允许 {'/'.join(allowed)}）")
        return v


@runtime_checkable
class AssessContextProvider(Protocol):
    """研判佐证来源的窄接口（retrieval/cases/graph/memory 的 MB 期门面）。缺位即空上下文。"""

    async def corroborate(self, *, region_code: str, hazard_hint: str | None, hits: list[Any]) -> dict[str, Any]: ...


@dataclass
class AssessAgent(AgentKit):
    context: AssessContextProvider | None = None
    #: 群体认知记忆门面（§4.3）：在场则读同区域历史研判进上下文、并把本次结论写回共享命名空间。
    memory: MemoryService | None = None
    double_blind: bool = True
    #: 双盲分歧仲裁：初值未实测（联调回填）。一致时置信度上调的幅度同样未实测。
    confidence_floor: float = 0.6
    confidence_boost: float = 0.1
    #: 双盲预算门槛（未实测初值）：deadline 低于此就不跑第二/三次评估——它是增强腿、非关键路径（§3.2）。
    double_blind_min_deadline_ms: int = 800
    #: 增强腿的预算倍数（未实测初值）：跑下一条腿前，要求剩余预算 ≥ 此倍 × 上一次真实调用耗时。
    #: 真机实测教训：只看请求 deadline（20s）等于永远不跳过——深言并发下单次 1.3–5.6s，
    #: 串行 2–3 次就把事务拖到 19.5s 撞死在 deadline 上（协同成功率因此掉到 0.81）。
    #: 用"已观测到的真实耗时"做门槛，慢的时候主动少跑增强腿并留痕，快的时候双盲照跑。
    blind_budget_factor: float = 1.5

    def _budget_left(self, request: AgentMessage, started_at: float) -> float:
        """剩余预算（ms）：请求 deadline 减去本次内环已花掉的时间；没给 deadline 就当宽裕。

        只用来决定要不要再花一次模型调用——增强腿可以少跑，关键路径（第一条结论）不能没有。
        """
        if request.deadline_ms is None:
            return float("inf")
        return request.deadline_ms - (time.perf_counter() - started_at) * 1000

    async def handle(self, request: AgentMessage) -> Reply:
        if self.llm is None:
            raise RuntimeError("assess 内环需要 LLM 网关（缺位由平台规则降级轨接管）")
        # 预算与网关同锚：从请求到达算起，不是从 handle 开始算（排队时间不是免费的）
        started_at = time.perf_counter() - self.elapsed_ms(request) / 1000
        payload = request.payload or {}
        region = str(payload.get("region_code", ""))
        hits = payload.get("hits") or []
        hazard_hint = self._first_hazard(hits)

        ctx: dict[str, Any] = {}
        if self.context is not None:
            ctx = await self.context.corroborate(region_code=region, hazard_hint=hazard_hint, hits=list(hits))

        # §3.2：同区域历史研判经共享记忆命名空间回注上下文（跨事件的经验闭环）。缺位/失败只降级不阻断。
        if self.memory is not None:
            try:
                prior = await self.memory.read_shared(kind="assessment", region_code=region or None, limit=3)
                if prior:
                    ctx = {**ctx, "memory": [record.content for record in prior]}
            except Exception as exc:
                log.warning("assess 历史研判召回降级", extra={"err": type(exc).__name__})

        base_vars = self._prompt_vars(region=region, hits=hits, ctx=ctx)
        first_started_at = time.perf_counter()
        first = await self.llm.chat_structured(
            [],
            schema=AssessVerdict,
            task="assess",
            prompt_key="assess",
            prompt_vars=base_vars,
            agent_id=self.agent_id,
            trace_id=request.trace_id,
        )
        first_ms = (time.perf_counter() - first_started_at) * 1000

        disagreement: bool | None = None
        final = first
        blind_status = "not_applicable"
        # 双盲只对高危（红=1/橙=2）触发（§8 成本对策）；一致则上调置信，分歧则第三 call 仲裁。
        if self.double_blind and int(first.risk_level) <= int(RiskLevel.ORANGE):
            deadline = request.deadline_ms
            if deadline is not None and deadline < self.double_blind_min_deadline_ms:
                # §3.2：双盲是增强腿、非关键路径——deadline 紧张时跳过并留痕，不冒超期风险。
                blind_status = "skipped_deadline"
            elif self._budget_left(request, started_at) < self.blind_budget_factor * first_ms:
                blind_status = "skipped_budget"
                log.info(
                    f"研判双盲增强腿跳过：剩余预算不足上一次调用的 {self.blind_budget_factor:.1f} 倍",
                    extra={"first_ms": round(first_ms, 1)},
                )
            else:
                # 增强腿的失败只能收口在增强腿：第一遍结论已经到手，第二/三遍任何异常都不许穿出
                # handle()——穿出就等于用一次模型故障废掉整段研判（真机 503 风暴实测：四条链路
                # stages 全成 mode=local、协同成功率 0.0，而第一遍结论本可以活下来）。
                leg = "second"
                try:
                    second_started_at = time.perf_counter()
                    second = await self.llm.chat_structured(
                        [],
                        schema=AssessVerdict,
                        task="arbitration",
                        prompt_key="assess",
                        prompt_vars=base_vars,
                        agent_id=self.agent_id,
                        trace_id=request.trace_id,
                    )
                    second_ms = (time.perf_counter() - second_started_at) * 1000
                    if int(second.risk_level) != int(first.risk_level):
                        arb_vars = self._prompt_vars(region=region, hits=hits, ctx=ctx, divergence=(first, second))
                        if self._budget_left(request, started_at) < self.blind_budget_factor * max(first_ms, second_ms):
                            # 分歧已经发生却没时间仲裁：取更保守（等级更高=数字更小）的一条，并留痕
                            final = first if int(first.risk_level) <= int(second.risk_level) else second
                            disagreement = True
                            blind_status = "arbitrated_skipped_budget"
                        else:
                            leg = "arbitration"
                            arb = await self.llm.chat_structured(
                                [],
                                schema=AssessVerdict,
                                task="arbitration",
                                prompt_key="assess",
                                prompt_vars=arb_vars,
                                agent_id=self.agent_id,
                                trace_id=request.trace_id,
                            )
                            final = arb
                            disagreement = True
                            blind_status = "arbitrated"
                    else:
                        final = first.model_copy(update={"confidence": min(0.99, first.confidence + self.confidence_boost)})
                        disagreement = False
                        blind_status = "agreed"
                except Exception as exc:
                    blind_status = f"failed_{leg}:{type(exc).__name__}"
                    log.warning(
                        "研判双盲增强腿失败，保留第一遍结论",
                        extra={"leg": leg, "err": type(exc).__name__, "trace_id": request.trace_id},
                    )

        result_payload = self._to_chain_payload(final, region=region, hits=hits)
        # §3.2 双盲状态如实留痕：agreed / arbitrated / skipped_deadline / skipped_budget /
        # arbitrated_skipped_budget / not_applicable —— 收腿这件事必须看得见，不能伪装成"跑了且一致"
        result_payload["double_blind"] = blind_status
        # 取数哪条腿没拿到（REST 503/超时/连不上）必须看得见，否则"没有佐证"会读成"佐证就是这些"。
        result_payload["context_degraded"] = list(ctx.get("degraded") or [])

        if self.memory is not None:
            try:
                await self.memory.write_shared(
                    kind="assessment",
                    agent_id=self.agent_id,
                    record_id=new_memory_record_id(),
                    content=f"{int(final.risk_level)}级/{final.hazard_type}：{final.rationale[:180]}",
                    region_code=region or None,
                    refs=[str(e) for e in (result_payload.get("evidence_refs") or [])][:8],
                    disagreement=bool(disagreement),
                    valid_at=now_iso(),
                )
            except Exception as exc:
                log.warning("assess 研判写记忆降级", extra={"err": type(exc).__name__})

        return Reply(payload=result_payload, model_info=self._provenance(), disagreement=disagreement)

    def _to_chain_payload(self, verdict: AssessVerdict, *, region: str, hits: list[Any]) -> dict[str, Any]:
        evidence = list(verdict.evidence_refs) or [self._rule_id(h) for h in hits]
        rationale = verdict.rationale
        if verdict.confidence < self.confidence_floor:
            # 低置信不拦发布（漏报代价高于误报），但如实标"建议人工核签"，交 assistant 人工闸承接（§3.2 护栏）。
            rationale = f"{rationale}（建议人工核签：置信度 {verdict.confidence:.2f} < {self.confidence_floor:.2f}）"
        return {
            "hazard_type": verdict.hazard_type,
            "risk_level": int(verdict.risk_level),
            "confidence": float(verdict.confidence),
            "rationale": rationale[:1000],
            "evidence_refs": [str(e) for e in evidence][:32],
            "assessed_by": self.agent_id,
            "human_review_suggested": bool(verdict.confidence < self.confidence_floor),
        }

    def _provenance(self) -> ModelInfo | None:
        getter = getattr(self.llm, "model_info_for_task", None)
        if getter is None:
            return None
        info = getter("assess")
        return ModelInfo(model=info.get("model"), prompt_version=info.get("prompt_version"))

    def _prompt_vars(
        self, *, region: str, hits: list[Any], ctx: dict[str, Any], divergence: tuple[AssessVerdict, AssessVerdict] | None = None
    ) -> dict[str, str]:
        """把上下文压成 PromptRegistry `assess` 模板的填充变量（正文单一真源在注册表，§4.1.6）。"""
        context = "".join(f"{label} 佐证：{ctx[label]}\n" for label in ("retrieval", "cases", "graph", "memory") if ctx.get(label))
        arbitration = ""
        if divergence is not None:
            a, b = divergence
            arbitration = (
                f"两次独立研判分歧：{int(a.risk_level)}级({a.rationale}) vs {int(b.risk_level)}级({b.rationale})，请给出最终仲裁等级。\n"
            )
        # 外部文本（触发命中携带的站点串、检索/案例/图谱/记忆佐证、分歧 rationale）过提示注入围栏（§4.8.2）。
        return {
            "region": region,
            # 枚举清单进提示词：只说"取枚举之一"而不给清单，模型就交回"暴雨诱发山洪、泥石流"这类
            # 中文描述——schema 现在会拒它（以前不拒，于是坏口径静默进下游）。同 plan 那条的教训。
            "hazards": "、".join(str(item.value) for item in HazardType),
            "hits": fence_untrusted("触发命中", str(list(hits)[:8])),
            "context": fence_untrusted("外部佐证", context) if context else "",
            "arbitration": fence_untrusted("分歧", arbitration) if arbitration else "",
        }

    @staticmethod
    def _first_hazard(hits: list[Any]) -> str | None:
        for h in hits:
            if isinstance(h, dict) and h.get("hazard_type"):
                value = str(h["hazard_type"])
                try:
                    return HazardType(value).value
                except ValueError:
                    return value
        return None

    @staticmethod
    def _rule_id(hit: Any) -> str:
        return str(hit.get("rule_id", "R-UNKNOWN")) if isinstance(hit, dict) else "R-UNKNOWN"
