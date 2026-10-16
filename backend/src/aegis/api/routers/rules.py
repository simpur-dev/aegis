"""规则面：当前生效的触发条件集、版本历史、标定对照，以及 §3.5 的阈值建议台账（列出 / 确认 / 驳回）。

这一族端点只出建议与读台账，**没有一条会自己改阈值**：生效的唯一路径是人工确认那一笔。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from aegis.api.deps import get_container
from aegis.container import PlatformContainer
from aegis.domain.enums import HazardType
from aegis.persistence.suggestions import SuggestionArgumentError, SuggestionConflictError
from aegis.services.rules.calibration import RuleSample, calibration_report, collect_rule_hits
from aegis.services.rules.threshold_suggestions import ThresholdSuggestionService
from aegis.storage.store import rule_library_port


class SuggestionConfirm(BaseModel):
    """人工确认阈值建议的载荷：**数字由确认的人给**。

    反馈智能体交出来的是一条理由（`threshold_suggestion` 是一段文本），把它解析成阈值
    就等于让一次 LLM 输出直接改动判据，而 §3.5 明写着"只进建议、落地走人工确认"。
    `expected_version` 必填：确认要对着"我看见的那一版"下手，版本已经被人换过就该重新对照，
    而不是让三周前打开的那一屏覆盖掉别人刚改的判断。
    `decided_by` 不接受默认值——接口不替人编一个签字人（与 D9 核签那条同源）。
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=3, max_length=48)
    metric: str = Field(min_length=1, max_length=64)
    threshold: float = Field(ge=0, le=1_000_000)
    expected_version: int = Field(ge=1)
    decided_by: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=500)


class SuggestionReject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decided_by: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=500)


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/rules", tags=["rules"])
    async def list_rules(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        """当前生效的触发条件集与它的出处。

        考核指标 1（识别 ≥5 类触发条件）的证据就是这一份：条数、灾种覆盖、版本号、
        标定依据与审核人都在，未标定的那些如实写着"未经现场标定"。
        `provenance.source` 区分"内置种子"与"规则库版本"，`error`/`rejected` 说明有没有换版失败。
        """
        engine = ctn.rule_engine
        rules = [rule.as_row() for rule in engine.rules]
        return {
            "provenance": engine.describe(),
            "load_error": ctn.rulebook_error,
            "rejected_rows": ctn.rulebook_rejected,
            "trigger_condition_kinds": len({condition.metric for rule in engine.rules for condition in rule.conditions}),
            "hazards_covered": sorted({rule.hazard_type.value for rule in engine.rules}),
            "items": rules,
        }

    @router.get("/api/v1/rules/versions", tags=["rules"], responses={503: {"description": "存储后端不提供规则库"}})
    async def list_rule_versions(
        ctn: PlatformContainer = Depends(get_container),
        status: Literal["draft", "active", "retired", "all"] = Query(default="all"),
        hazard_type: HazardType | None = None,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        """规则库的版本历史（含草稿与退役）：只有 PostgreSQL 形态有这一面。

        内存读视图没有"版本化阈值"这件事，内置种子就是唯一版本——所以这里如实 503，
        而不是把当前生效集当作"v1 版本清单"再报一遍。
        """
        port = rule_library_port(ctn.store)
        if port is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "E_RULEBOOK_UNAVAILABLE",
                    "message": "当前存储后端不提供规则库版本面",
                    "requires": "AEGIS_STORE_BACKEND=postgres（表 trigger_rules）",
                },
            )
        rows = await port.trigger_rules(status=status, hazard_type=None if hazard_type is None else hazard_type.value, limit=limit)
        return {"status": status, "count": len(rows), "items": rows}

    @router.get("/api/v1/rules/calibration", tags=["rules"])
    async def rules_calibration(
        ctn: PlatformContainer = Depends(get_container),
        limit: int = Query(default=2_000, ge=1, le=10_000, description="回看多少条链路台账样本"),
    ) -> dict[str, Any]:
        """分规则命中与误报对照（批次 C3）。**只出建议，不改阈值。**

        命中次数取自平台链路台账（实测）；精度需要现场真值配对（批次 E1），缺就写
        `not_measured`。报表里 `auto_applied` 恒为 false——阈值生效的唯一路径是
        人工确认（`POST /api/v1/feedback/suggestions/{id}/confirm`）把新版本写进
        `trigger_rules`，历史读 `/api/v1/rules/versions`。
        """
        rows = list(ctn.store.chains.latest(int(limit)))
        hits = collect_rule_hits(rows)
        samples = {
            rule_id: RuleSample(rule_id=rule_id, hits=count)
            for rule_id, count in hits.items()
            if ctn.rule_engine.rule_by_id(rule_id) is not None
        }
        report = calibration_report(ctn.rule_engine.rules, samples, dataset_kind="unspecified", dataset_cases=0)
        return {
            **report.as_dict(),
            "ledger_rows": len(rows),
            "report_rulebook": ctn.rule_engine.describe(),
            "off_bookrule_hits": {key: value for key, value in hits.items() if ctn.rule_engine.rule_by_id(key) is None},
        }

    def _suggestion_service(ctn: PlatformContainer) -> ThresholdSuggestionService:
        """三个建议端点共用这一句"这条腿装了没有"，因为漏装的表现是一样的静默。"""
        service = ctn.suggestions
        if service is None or not service.available:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "E_SUGGESTION_LEDGER_UNAVAILABLE",
                    "message": "当前装配没有阈值建议台账",
                    "requires": "AEGIS_STORE_BACKEND=postgres（表 threshold_suggestions 与 trigger_rules 版本面）",
                },
            )
        return service

    @router.get(
        "/api/v1/feedback/suggestions",
        tags=["rules"],
        responses={503: {"description": "存储后端没有建议台账"}},
    )
    async def list_threshold_suggestions(
        ctn: PlatformContainer = Depends(get_container),
        status: Literal["pending_human_review", "confirmed", "rejected", "all"] = Query(default="pending_human_review"),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        """§3.5 的后半截从此有了读者：建议不再只是段注里的一段话，而是能列出来待办的一行。

        默认只看待确认的。`all` 是给审计看的（谁在什么时候把哪条驳回成了什么）。
        """
        service = _suggestion_service(ctn)
        rows = await service.list(status=status, limit=limit)
        return {"status": status, "count": len(rows), "items": rows}

    @router.post(
        "/api/v1/feedback/suggestions/{suggestion_id}/confirm",
        tags=["rules"],
        responses={409: {"description": "这条建议已被裁决，或规则不是提交时看到的那一版"}, 503: {"description": "存储后端没有建议台账"}},
    )
    async def confirm_threshold_suggestion(
        suggestion_id: str,
        payload: SuggestionConfirm,
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """人工确认落地：规则库长出一个新版本，建议同时销账，两件事在同一笔事务里。

        数字由确认的人给，不是由模型给——反馈智能体交出来的是一条**理由**（`threshold_suggestion`
        是一段文本），把它解析成阈值就等于让一次 LLM 输出直接改动判据，而 §3.5 明写着"只进建议"。
        `expected_version` 是防"凭三周前看到的 v2 覆盖掉别人刚改的 v5"。
        `engine_loaded` 单独说：库里版本已经落，而内存里那台引擎有没有换上，是两件事。
        """
        service = _suggestion_service(ctn)
        try:
            return await service.confirm(
                suggestion_id,
                rule_id=payload.rule_id,
                metric=payload.metric,
                threshold=payload.threshold,
                decided_by=payload.decided_by,
                note=payload.note,
                expected_version=payload.expected_version,
            )
        except SuggestionConflictError as exc:
            raise HTTPException(status_code=409, detail={"code": "E_SUGGESTION_DECIDED", "message": exc.message, **exc.detail}) from exc
        except SuggestionArgumentError as exc:
            raise HTTPException(status_code=422, detail={"code": "E_SUGGESTION_ARG", "message": str(exc), **exc.detail}) from exc

    @router.post(
        "/api/v1/feedback/suggestions/{suggestion_id}/reject",
        tags=["rules"],
        responses={409: {"description": "这条建议已被裁决"}, 503: {"description": "存储后端没有建议台账"}},
    )
    async def reject_threshold_suggestion(
        suggestion_id: str,
        payload: SuggestionReject,
        ctn: PlatformContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """驳回也要留痕：一条被驳回的建议如果只从队列里消失，下一个人无法知道它被看过想过。"""
        service = _suggestion_service(ctn)
        try:
            return await service.reject(suggestion_id, decided_by=payload.decided_by, note=payload.note)
        except SuggestionConflictError as exc:
            raise HTTPException(status_code=409, detail={"code": "E_SUGGESTION_DECIDED", "message": exc.message, **exc.detail}) from exc
        except SuggestionArgumentError as exc:
            raise HTTPException(status_code=422, detail={"code": "E_SUGGESTION_ARG", "message": str(exc), **exc.detail}) from exc

    return router
