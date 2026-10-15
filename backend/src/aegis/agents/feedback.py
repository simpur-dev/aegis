"""feedback 反馈智能体内环（§3.5）：回执/响应态汇总 → LLM 归因 → 阈值修正【建议】+ 教训写记忆。

R9 的落点，也是"预警反馈闭环"的智能体侧一半。诚实边界（对齐铁律 7 / 数字防幻觉）：
- `effect_score` 由**给定回执数据确定性地算出**，不经 LLM、不臆造——口径写死在本文件（§3.5 验收：口径文档化）。
- LLM 只做定性归因（哪类受众未触达、迟滞环节）与一条可复用教训；LLM 不可用/不合规 → 回落规则归因摘要，
  绝不因缺模型而丢状态输出（本内环恒产出 feedback.status）。
- 阈值/传播**只进建议**（`suggestion_status=pending_human_review`），落地必须走人工确认 → calibration 复用；
  本智能体不改阈值。
- 教训经注入的 MemoryService 写入共享命名空间（§4.3 的第二个消费者）；写失败只降级不断链。

集成现状：链路在回执汇总后向 `agent.feedback.in` 派发一次 `feedback.attribute`（`pipeline/chain.py`
的 `_feedback`），采纳的归因会带进段台账与 ops 状态事件；失败或应答不合契约时按降级留痕、不改结论。
总线级的派发与消费由 `tests/contract/test_feedback_closed_loop.py` 钉住——这条腿曾经只广播不派发，
于是本内环在生产链上 0 次发生，而单测直调 `handle()` 看不出来。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from aegis.agents.agentkit import AgentKit, Reply
from aegis.domain.messages import AgentMessage, ModelInfo, now_iso
from aegis.errors import AegisError
from aegis.services.memory import MemoryService, new_memory_record_id
from aegis.services.prompt_guard import fence_untrusted

log = logging.getLogger("aegis.agents.feedback")

#: response_state → 响应系数（确定性口径，未实测的相对权重，联调后可标定；不含任何"实测达标"含义）。
_RESPONSE_FACTOR = {
    "acknowledged": 1.0,
    "responded": 0.8,
    "partial": 0.5,
    "pending": 0.4,
    "unknown": 0.2,
    "none": 0.0,
    "": 0.3,
}


class FeedbackAttribution(BaseModel):
    """LLM 定性归因的结构（不含任何由模型发明的数字；数字一律来自回执）。"""

    model_config = {"extra": "forbid"}

    attribution: str = Field(min_length=1, max_length=500)
    unreached_audiences: list[str] = Field(default_factory=list)
    lag_stage: str = Field(default="", max_length=120)
    effect_basis: str = Field(default="", max_length=500)
    threshold_suggestion: str = Field(default="", max_length=500)
    lesson: str = Field(min_length=1, max_length=2_000)


@dataclass
class FeedbackAgent(AgentKit):
    memory: MemoryService | None = None

    async def handle(self, request: AgentMessage) -> Reply:
        payload = request.payload or {}
        warning_id = str(payload.get("warning_id", ""))
        raw_reach = payload.get("reach_stats")
        reach: dict[str, Any] = raw_reach if isinstance(raw_reach, dict) else {}
        delivered = int(reach.get("delivered", 0) or 0)
        failed = int(reach.get("failed", 0) or 0)
        pending = int(reach.get("pending", 0) or 0)
        total = int(reach.get("total", 0) or 0) or (delivered + failed + pending)
        response_state = str(payload.get("response_state", "unknown"))
        reports = payload.get("field_reports") or []
        region = payload.get("region_code") or None

        # 口径：效果分 = 触达率×0.7 + 响应系数×0.3，全部来自给定回执，不经 LLM。
        reach_ratio = delivered / total if total > 0 else 0.0
        factor = _RESPONSE_FACTOR.get(response_state, 0.3)
        effect_score = round(min(1.0, max(0.0, 0.7 * reach_ratio + 0.3 * factor)), 3)

        attribution, is_llm = await self._attribute(
            request, warning_id=warning_id, delivered=delivered, total=total, response_state=response_state, reports=reports
        )

        result: dict[str, Any] = {
            "warning_id": warning_id,
            "reach_stats": {"delivered": delivered, "failed": failed, "pending": pending, "total": total},
            "response_state": response_state,
            "effect_score": effect_score,
            "attribution": attribution.attribution,
            "unreached_audiences": attribution.unreached_audiences[:16],
            "lag_stage": attribution.lag_stage,
            "threshold_suggestion": attribution.threshold_suggestion,
            "suggestion_status": "pending_human_review" if attribution.threshold_suggestion else "none",
            "decided_by": "agent" if is_llm else "local",
        }

        if self.memory is not None:
            try:
                await self.memory.write_shared(
                    kind="lesson",
                    agent_id=self.agent_id,
                    record_id=new_memory_record_id(),
                    content=attribution.lesson,
                    region_code=region,
                    refs=[warning_id] if warning_id else [],
                    valid_at=now_iso(),
                )
            except Exception as exc:
                log.warning("feedback 教训写记忆降级", extra={"err": type(exc).__name__})

        return Reply(payload=result, model_info=self._provenance())

    async def _attribute(
        self, request: AgentMessage, *, warning_id: str, delivered: int, total: int, response_state: str, reports: list[Any]
    ) -> tuple[FeedbackAttribution, bool]:
        if self.llm is not None and getattr(self.llm, "available", True):
            try:
                model_out = await self.llm.chat_structured(
                    [],
                    schema=FeedbackAttribution,
                    task="feedback",
                    prompt_key="feedback",
                    prompt_vars={
                        "warning_line": f"预警：{warning_id} 触达 {delivered}/{total} 响应态 {response_state}",
                        "reach": str({"delivered": delivered, "total": total}),
                        "response_state": response_state,
                        "reports": fence_untrusted("现场上报", str(list(reports)[:5])),
                    },
                    agent_id=self.agent_id,
                    trace_id=request.trace_id,
                )
                return model_out, True
            except AegisError as exc:
                log.warning("feedback LLM 归因降级为规则摘要", extra={"err": type(exc).__name__})
        # 规则回落：只用回执里的确定性事实，不编造归因。
        summary = f"规则归因：触达 {delivered}/{total}，响应态 {response_state}（LLM 不可用，未做受众级细分）。"
        return FeedbackAttribution(
            attribution=summary, lesson=f"{warning_id or '预警'}：触达 {delivered}/{total}，响应 {response_state}。"
        ), False

    def _provenance(self) -> ModelInfo | None:
        getter = getattr(self.llm, "model_info_for_task", None)
        if getter is None:
            return None
        info = getter("feedback")
        return ModelInfo(model=info.get("model"), prompt_version=info.get("prompt_version"))
