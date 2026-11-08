"""L4 规则控制动作：确认建议 / 驳回建议 / 按显式数字直改阈值。

沿用 `api/routers/rules.py` 那条铁律：**阈值数字由确认的人给，不由模型给**。
三个动作的参数都是人显式写出的 `rule_id / metric / threshold / version`，动作层不解析建议
文本、不猜当前版本；`decided_by` 取当前席位，不接受模型填写。所有落库仍走
`ThresholdSuggestionService` 的同一笔事务（建议销账 + 规则长新版本 + 引擎换集），
`rule.update` 也不开直改路径：先写一条 pending 建议，再立即确认它。
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aegis.domain.operator import OperatorRole
from aegis.services.assistant.base import (
    Action,
    ActionContext,
    ActionDomain,
    ActionError,
    ActionResult,
    ActionSpec,
    ImpactPreview,
    ResolvedTargets,
    TargetRef,
)

_SUGGESTION_ID = r"^sug_[0-9a-f]{6,24}$"
_METRIC = r"^[a-z][a-z0-9_]{0,63}$"

#: 架构铁律：`services/` 不得 import 可选腿包（`persistence/`），连延迟导入也不行。
#: 因此这里按异常类名把持久层的三种业务语义映射成动作错误码，映射只此一处、由单测钉住。
#: 未列出的异常交给执行器归一为 `E_ACTION_EXECUTION_FAILED`，不会被吞成"成功"。
_RULE_ERROR_CODES = {
    "SuggestionConflictError": "E_ACTION_CONFLICT",
    "SuggestionArgumentError": "E_ACTION_PARAM_INVALID",
    "LedgerUnavailableError": "E_RULE_LEDGER_UNAVAILABLE",
}


def _translate(exc: Exception) -> ActionError:
    code = _RULE_ERROR_CODES.get(type(exc).__name__, "E_ACTION_EXECUTION_FAILED")
    return ActionError(code, str(exc)[:300])


class RulePort(Protocol):
    """阈值建议服务在动作侧的形状：`ThresholdSuggestionService` 直接实现它。"""

    @property
    def available(self) -> bool: ...

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
    ) -> dict[str, Any]: ...

    async def reject(self, suggestion_id: str, *, decided_by: str, note: str = "") -> dict[str, Any]: ...

    async def apply(
        self,
        *,
        rule_id: str,
        metric: str,
        threshold: float,
        decided_by: str,
        note: str = "",
        expected_version: int,
    ) -> dict[str, Any]: ...


class RuleTargetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestion_id: str = Field(pattern=_SUGGESTION_ID)


class RuleConfirmParams(RuleTargetParams):
    rule_id: str = Field(min_length=1, max_length=64)
    metric: str = Field(pattern=_METRIC)
    threshold: float
    expected_version: int = Field(ge=1)
    note: str = Field(default="", max_length=200)


class RuleRejectParams(RuleTargetParams):
    note: str = Field(default="", max_length=200)


class RuleUpdateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1, max_length=64)
    metric: str = Field(pattern=_METRIC)
    threshold: float
    expected_version: int = Field(ge=1)
    note: str = Field(default="", max_length=200)


class _RuleAction(Action):
    def __init__(self, rules: RulePort | None) -> None:
        self._rules = rules

    def _require(self) -> RulePort:
        if self._rules is None or not self._rules.available:
            raise ActionError(
                "E_RULE_LEDGER_UNAVAILABLE",
                "阈值建议台账未装配（需要 AEGIS_STORE_BACKEND=postgres），规则动作不可用",
            )
        return self._rules

    @staticmethod
    def _operator(ctx: ActionContext) -> str:
        if ctx.operator is None:
            raise ActionError("E_OPERATOR_REQUIRED", "规则动作需要有效席位（X-Aegis-Operator）")
        return ctx.operator.operator_id

    async def preview(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ImpactPreview:
        return ImpactPreview(
            targets=targets.items,
            irreversible=(f"「{self.spec.title}」不可撤销：规则库会长出一个新版本，旧版本只退役不删除",),
            warnings=("阈值与版本由操作者显式给出；与目标规则当前版本不一致时会被 409 拒绝",),
        )

    @staticmethod
    def _brief(outcome: dict[str, Any]) -> dict[str, Any]:
        suggestion = outcome.get("suggestion") or {}
        keys = ("suggestion_id", "status", "rule_id", "new_version", "decided_by")
        brief = {key: suggestion[key] for key in keys if suggestion.get(key) is not None}
        return {
            "suggestion": brief,
            "rule_id": outcome.get("rule_id", ""),
            "new_version": outcome.get("new_version"),
            "engine_loaded": outcome.get("engine_loaded"),
            "engine_note": outcome.get("engine_note", ""),
        }

    @staticmethod
    def _reject_brief(row: dict[str, Any]) -> dict[str, Any]:
        keys = ("suggestion_id", "status", "decided_by", "decision_note")
        return {key: row[key] for key in keys if row.get(key) is not None}


class RuleConfirmSuggestion(_RuleAction):
    spec = ActionSpec(
        name="rule.confirm_suggestion",
        title="确认阈值建议",
        domain=ActionDomain.RULE,
        required_role=OperatorRole.LEAD,
        params=RuleConfirmParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
        example="确认阈值建议 suggestion_id=sug_abcdef123456 rule_id=rain-01 metric=rainfall_1h threshold=75 version=2",
    )

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, RuleConfirmParams)
        return ResolvedTargets((TargetRef("threshold_suggestion", params.suggestion_id, params.rule_id),))

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, RuleConfirmParams)
        rules = self._require()
        try:
            outcome = await rules.confirm(
                params.suggestion_id,
                rule_id=params.rule_id,
                metric=params.metric,
                threshold=params.threshold,
                decided_by=self._operator(ctx),
                note=params.note,
                expected_version=params.expected_version,
            )
        except Exception as exc:
            raise _translate(exc) from exc
        return ActionResult(data=self._brief(outcome), summary=f"建议 {params.suggestion_id} 已确认落地")


class RuleRejectSuggestion(_RuleAction):
    spec = ActionSpec(
        name="rule.reject_suggestion",
        title="驳回阈值建议",
        domain=ActionDomain.RULE,
        required_role=OperatorRole.LEAD,
        params=RuleRejectParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
        example="驳回阈值建议 suggestion_id=sug_abcdef123456 note=样本不足",
    )

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, RuleRejectParams)
        return ResolvedTargets((TargetRef("threshold_suggestion", params.suggestion_id),))

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, RuleRejectParams)
        rules = self._require()
        try:
            row = await rules.reject(params.suggestion_id, decided_by=self._operator(ctx), note=params.note)
        except Exception as exc:
            raise _translate(exc) from exc
        return ActionResult(
            data={"suggestion": self._reject_brief(row)},
            summary=f"建议 {params.suggestion_id} 已驳回",
        )


class RuleUpdate(_RuleAction):
    spec = ActionSpec(
        name="rule.update",
        title="按显式阈值调整规则",
        domain=ActionDomain.RULE,
        required_role=OperatorRole.ADMIN,
        params=RuleUpdateParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
        example="改阈值 rule_id=rain-01 metric=rainfall_1h threshold=80 version=2 note=汛期上调",
    )

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, RuleUpdateParams)
        return ResolvedTargets((TargetRef("trigger_rule", params.rule_id, params.metric),))

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, RuleUpdateParams)
        rules = self._require()
        try:
            outcome = await rules.apply(
                rule_id=params.rule_id,
                metric=params.metric,
                threshold=params.threshold,
                decided_by=self._operator(ctx),
                note=params.note,
                expected_version=params.expected_version,
            )
        except Exception as exc:
            raise _translate(exc) from exc
        return ActionResult(
            data=self._brief(outcome),
            summary=f"规则 {params.rule_id}.{params.metric} 阈值已按显式数字调整（走建议+确认同一事务）",
        )
