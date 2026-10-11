"""assess 研判智能体内环（§3.2）：LLM 结构化定级 + 双盲交叉验证 + D10 溯源。

R1/R8/R11 的落点：定级由 `chat_structured`（1–5 枚举强约束，越界即拒）产出，红/橙级触发
双盲（第二评估 + 分歧仲裁），结论自带 `model_info`（可复现）与 `disagreement`。取数经注入的
`AssessContextProvider`（Protocol），**不碰存储层**；LLM 缺位/超时/不合规一律让内环异常上抛，
由 agentkit 回执 error、平台规则降级轨接管（LLM 不可用≠平台不可用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from aegis.agents.agentkit import AgentKit, Reply
from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import AgentMessage, ModelInfo


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

        messages = self._build_messages(region=region, hits=hits, ctx=ctx)
        first = await self.llm.chat_structured(
            messages, schema=AssessVerdict, task="assess", agent_id=self.agent_id, trace_id=request.trace_id
        )

        disagreement: bool | None = None
        final = first
        # 双盲只对高危（红=1/橙=2）触发（§8 成本对策）；一致则上调置信，分歧则第三 call 仲裁。
        if self.double_blind and int(first.risk_level) <= int(RiskLevel.ORANGE):
            second = await self.llm.chat_structured(
                messages, schema=AssessVerdict, task="arbitration", agent_id=self.agent_id, trace_id=request.trace_id
            )
            if int(second.risk_level) != int(first.risk_level):
                arb = await self.llm.chat_structured(
                    self._build_messages(region=region, hits=hits, ctx=ctx, arbitration=(first, second)),
                    schema=AssessVerdict,
                    task="arbitration",
                    agent_id=self.agent_id,
                    trace_id=request.trace_id,
                )
                final = arb
                disagreement = True
            else:
                final = first.model_copy(update={"confidence": min(0.99, first.confidence + self.confidence_boost)})
                disagreement = False

        result_payload = self._to_chain_payload(final, region=region, hits=hits)
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

    def _build_messages(
        self, *, region: str, hits: list[Any], ctx: dict[str, Any], arbitration: tuple[AssessVerdict, AssessVerdict] | None = None
    ) -> list[dict[str, str]]:
        system = (
            "你是山地灾害风险研判专家。只输出一个 JSON 对象，字段为 risk_level(整数1-5，1最重5最轻)、"
            "hazard_type、confidence(0-1)、rationale(中文，引用佐证不臆造数字)、evidence_refs(字符串数组)。"
            "禁止发明任何未出现在佐证里的数字。"
        )
        parts = [f"区域：{region}", f"触发命中：{hits[:8]}"]
        for label in ("retrieval", "cases", "graph", "memory"):
            if ctx.get(label):
                parts.append(f"{label} 佐证：{ctx[label]}")
        if arbitration is not None:
            a, b = arbitration
            parts.append(f"两次独立研判分歧：{int(a.risk_level)}级({a.rationale}) vs {int(b.risk_level)}级({b.rationale})，请仲裁其一。")
        user = "\n".join(parts)
        if arbitration is not None:
            user += "\n请给出最终仲裁等级。"
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

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
