"""把知识层与检索层的命中折成解析器认识的 `EvidenceNote`（装配点专属翻译，内核只认 Protocol）。

（P4 纯搬运：类体与 `_worst_level` 逐字未改，原先住在 `container.py`。）
上报文本的三路融合解析里，"佐证"这一路要同时读图谱案例与混合检索块；两边的分数、预算与
降级判据都在可选腿那侧，所以翻译必须留在装配层——内核拿到的只是一个 `corroborate` 可调用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aegis.config import Settings
from aegis.integrations.shared import log
from aegis.knowledge.provider import KnowledgeProvider
from aegis.services.interpret.semantic_parser import EvidenceNote

if TYPE_CHECKING:
    from aegis.retrieval.service import HybridRetrievalService


class AssembledEvidence:
    """把知识层 + 检索层折成解析器认识的 `EvidenceNote`（装配点专属翻译，内核只认 Protocol）。

    `typical_level` 只取案例自带的 `applies_to_levels` 中最不利（数字最小）的一档；
    检索文档没有等级就不给等级——编一个等级出来，等于把"历史案例"降级成"我们的猜测"。
    """

    def __init__(self, *, knowledge: KnowledgeProvider | None, retrieval: HybridRetrievalService | None, settings: Settings) -> None:
        self._knowledge = knowledge
        self._retrieval = retrieval
        self._settings = settings

    async def corroborate(
        self,
        *,
        query: str,
        hazard_type: str | None,
        region_code: str | None,
        limit: int,
    ) -> list[EvidenceNote]:
        notes: list[EvidenceNote] = []
        if self._knowledge is not None:
            try:
                matches = await self._knowledge.recall(
                    query,
                    hazard_type=hazard_type,
                    region_code=region_code,
                    limit=limit,
                    budget_ms=self._settings.knowledge_recall_budget_ms,
                )
            except Exception as exc:
                log.warning("解析佐证：知识腿降级", extra={"err": type(exc).__name__})
                matches = []
            for match in matches:
                brief = match.planning_brief()
                notes.append(
                    EvidenceNote(
                        source=f"case:{match.case_id}",
                        text=str(brief.get("title") or match.case_id)[:200],
                        hazard_type=next(iter(brief.get("hazard_types") or ()), hazard_type),
                        typical_level=_worst_level(brief.get("applies_to_levels")),
                        refs=(match.case_id,),
                    )
                )
        if self._retrieval is not None:
            from aegis.retrieval.service import RetrievalQuery  # 延迟导入：装配点才碰检索 I/O 面

            try:
                outcome = await self._retrieval.retrieve(
                    RetrievalQuery(
                        text=query,
                        k=limit,
                        hazard_type=hazard_type,
                        region_code=region_code,
                        budget_ms=self._settings.retrieval_budget_ms,
                    )
                )
            except Exception as exc:
                log.warning("解析佐证：检索腿降级", extra={"err": type(exc).__name__})
                outcome = None
            if outcome is not None:
                for doc in outcome.docs:
                    reference = doc.as_reference()
                    notes.append(
                        EvidenceNote(
                            source=f"doc:{doc.doc_id}",
                            text=str(reference.get("text") or reference.get("title") or "")[:200],
                            hazard_type=reference.get("hazard_type"),
                            typical_level=None,  # 检索块没有等级口径，就不臆造
                            refs=(doc.doc_id,),
                        )
                    )
        return notes


def _worst_level(raw: object) -> int | None:
    levels = [int(item) for item in raw if isinstance(item, (int, float)) and 1 <= int(item) <= 5] if isinstance(raw, list) else []
    return min(levels) if levels else None
