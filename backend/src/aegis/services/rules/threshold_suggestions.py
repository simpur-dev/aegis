"""阈值建议闭环的服务侧（§3.5 后半截）：入账、待确认清单、确认落地、驳回。

为什么要有这一层，而不是让端点直接调持久层：**确认这件事同时牵动三方**——台账销账、
`trigger_rules` 长出一个新版本、内存里那台正在判级的规则引擎要不要跟着换集。
把"换不换引擎"放进 `persistence/` 会让持久层反过来依赖判据服务，放进 `api/` 又会让
第二个消费方（比如以后的 MCP 工具面）各写一遍。所以它住在这里，只住这一处。

铁律 3 在本层的形状：这里只认 `domain` 的行模型与 `storage` 的能力协议，
**不 import `persistence`**。SQL 与事务住在可选腿里，缺那条腿时本层如实报"台账不可用"。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from aegis.domain.messages import ThresholdSuggestion, new_suggestion_id
from aegis.services.rules.trigger_rules import RuleEngine, rulebook_from_rows
from aegis.storage.store import SupportsRuleLibrary, SupportsThresholdSuggestions, rule_library_port, suggestion_ledger_port

#: 台账里 `status` 的默认读法：值班员要看的从来都是"还挂着的那些"。
PENDING = "pending_human_review"


class LedgerUnavailableError(RuntimeError):
    """这条腿没装就想读写台账。HTTP 侧应当先问 `available` 并回 503；留着这一句是给别的调用方。"""


class ThresholdSuggestionService:
    """阈值建议的读写口 + "确认之后让新阈值真的生效"。"""

    def __init__(self, *, store: object, rule_engine: RuleEngine) -> None:
        self._rule_engine = rule_engine
        self._ledger: SupportsThresholdSuggestions | None = suggestion_ledger_port(store)
        self._rulebook: SupportsRuleLibrary | None = rule_library_port(store)

    @property
    def available(self) -> bool:
        return self._ledger is not None

    @property
    def unavailable_reason(self) -> str:
        if self._ledger is not None:
            return ""
        return "当前存储后端没有阈值建议台账（建议与规则库版本是同一件事，内存读视图里两者都不存在）"

    async def record(
        self,
        *,
        trace_id: str,
        attribution: dict[str, Any],
        warning_id: str = "",
        region_code: str = "",
        hazard_type: str = "",
    ) -> str:
        """把一条 `pending_human_review` 的建议写进台账，返回一句**状态**而不是布尔。

        返回值要能直接进段注：生产链路上"没入账"有三种原因（没这张表 / 载荷不合契约 /
        库里写不进去），把它们都说成 False 就等于把降级藏进一个布尔里（铁律 7）。
        """
        suggestion = str(attribution.get("threshold_suggestion", "")).strip()
        if suggestion == "":
            return "skipped_empty"
        if self._ledger is None:
            return "skipped_no_ledger"
        try:
            row = ThresholdSuggestion(
                suggestion_id=new_suggestion_id(),
                trace_id=trace_id,
                warning_id=warning_id,
                region_code=region_code,
                hazard_type=hazard_type,
                suggestion=suggestion,
                attribution=str(attribution.get("attribution", "")),
                lag_stage=str(attribution.get("lag_stage", "")),
                # 效果分缺席就传 None：0.0 是一个结论，None 是一个事实
                effect_score=attribution.get("effect_score"),
            ).as_row()
        except ValidationError:
            return "rejected_payload"
        try:
            written = await self._ledger.put_threshold_suggestion(row)
        except Exception as exc:
            return f"failed_{type(exc).__name__}"
        return f"stored:{written.get('suggestion_id', '')}"

    async def list(self, *, status: str = PENDING, limit: int = 100) -> list[dict[str, Any]]:
        ledger = self._ledger
        if ledger is None:
            raise LedgerUnavailableError(self.unavailable_reason)
        return await ledger.threshold_suggestions(status=status, limit=limit)

    async def confirm(
        self,
        suggestion_id: str,
        *,
        rule_id: str,
        metric: str,
        threshold: float,
        decided_by: str,
        note: str = "",
        expected_version: int,
    ) -> dict[str, Any]:
        """确认落地：先写库，再让引擎装载新集合，两件事的结果**分开说**。

        顺序不能反。先换引擎再写库的话，库里那一笔一旦回滚，内存里已经按新阈值判过级了
        ——报表会出现"库说 v3、引擎按 v4 出结论"这种两边都对不上的形状。反过来（库成了、
        引擎没换上）是一个**可见的中间态**：`engine_loaded=false` 加一句原因，值班员看得见
        "这一票已经记进库，但还在用旧阈值判"。
        """
        ledger = self._ledger
        if ledger is None:
            raise LedgerUnavailableError(self.unavailable_reason)
        decided = await ledger.confirm_threshold_suggestion(
            suggestion_id=suggestion_id,
            rule_id=rule_id,
            metric=metric,
            threshold=threshold,
            decided_by=decided_by,
            note=note,
            expected_version=expected_version,
        )
        engine = await self.reload_engine()
        return {"suggestion": decided, "rule_id": rule_id, "new_version": decided.get("new_version"), **engine}

    async def reject(self, suggestion_id: str, *, decided_by: str, note: str = "") -> dict[str, Any]:
        ledger = self._ledger
        if ledger is None:
            raise LedgerUnavailableError(self.unavailable_reason)
        return await ledger.reject_threshold_suggestion(suggestion_id=suggestion_id, decided_by=decided_by, note=note)

    async def reload_engine(self) -> dict[str, Any]:
        """从库里重读 active 规则集并装载；结果说清"装上了没有、装的是哪一版"。"""
        if self._rulebook is None:
            return {"engine_loaded": False, "engine_note": "存储后端不提供规则库版本面", "engine": self._rule_engine.describe()}
        try:
            rows = await self._rulebook.trigger_rules(status="active", limit=200)
        except Exception as exc:
            return {"engine_loaded": False, "engine_note": f"规则库重读失败: {type(exc).__name__}", "engine": self._rule_engine.describe()}
        rules, rejected = rulebook_from_rows(rows)
        try:
            loaded = self._rule_engine.reload(rules, source="postgres")
        except ValueError as exc:
            return {"engine_loaded": False, "engine_note": f"引擎拒绝装载: {str(exc)[:160]}", "engine": self._rule_engine.describe()}
        return {
            "engine_loaded": True,
            "engine_note": f"装载 {loaded} 条，坏行 {len(rejected)} 条" if rejected else f"装载 {loaded} 条",
            "engine": self._rule_engine.describe(),
        }
