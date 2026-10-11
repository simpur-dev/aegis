"""assess 研判智能体内环（§3.2）：LLM 结构化定级 + 双盲交叉验证 + D10 溯源。

R1/R8/R11 的落点：定级由 `chat_structured`（1–5 枚举强约束，越界即拒）产出，红/橙级触发
双盲（第二评估 + 分歧仲裁），结论自带 `model_info`（可复现）与 `disagreement`。取数经注入的
`AssessContextProvider`（Protocol），**不碰存储层**；LLM 缺位/超时/不合规一律让内环异常上抛，
由 agentkit 回执 error、平台规则降级轨接管（LLM 不可用≠平台不可用）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from aegis.agents.agentkit import AgentKit, Reply
from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import AgentMessage, ModelInfo, now_iso
from aegis.services.memory import MemoryService, new_memory_record_id

log = logging.getLogger("aegis.agents.assess")


class AssessVerdict(BaseModel):
    """LLM 结构化定级的目标 schema：1–5 枚举冻结、置信度 0..1、rationale/evidence 必填可溯源。"""

    model_config = {"extra": "forbid"}

    risk_level: int = Field(ge=1, le=5)
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

    async def handle(self, request: AgentMessage) -> Reply:
        if self.llm is None:
            raise RuntimeError("assess 内环需要 LLM 网关（缺位由平台规则降级轨接管）")
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
        first = await self.llm.chat_structured(
            [],
            schema=AssessVerdict,
            task="assess",
            prompt_key="assess",
            prompt_vars=base_vars,
            agent_id=self.agent_id,
            trace_id=request.trace_id,
        )

        disagreement: bool | None = None
        final = first
        # 双盲只对高危（红=1/橙=2）触发（§8 成本对策）；一致则上调置信，分歧则第三 call 仲裁。
        if self.double_blind and int(first.risk_level) <= int(RiskLevel.ORANGE):
            second = await self.llm.chat_structured(
                [],
                schema=AssessVerdict,
                task="arbitration",
                prompt_key="assess",
                prompt_vars=base_vars,
                agent_id=self.agent_id,
                trace_id=request.trace_id,
            )
            if int(second.risk_level) != int(first.risk_level):
                arb_vars = self._prompt_vars(region=region, hits=hits, ctx=ctx, divergence=(first, second))
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
            else:
                final = first.model_copy(update={"confidence": min(0.99, first.confidence + self.confidence_boost)})
                disagreement = False

        result_payload = self._to_chain_payload(final, region=region, hits=hits)

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
        return {"region": region, "hits": str(list(hits)[:8]), "context": context, "arbitration": arbitration}

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
