"""智能体面：在册清单与网关计数、协同事务与成功率、模型卡片。

智能体不通过 HTTP 交互（一律走总线契约），这里只有观测与台账。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from aegis.api.deps import get_container
from aegis.container import PlatformContainer


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/agents", tags=["agent"])
    async def list_agents(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        return {
            "online": ctn.registry.online_count,
            "items": ctn.registry.snapshot(),
            "gateway_counters": dict(ctn.gateway.counters),
        }

    @router.get("/api/v1/collaboration", tags=["agent"])
    async def collaboration(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        return {
            "success_rate": ctn.gateway.success_rate(),
            "transactions": [r.as_dict() for r in ctn.gateway.transactions[-200:]],
        }

    @router.get("/api/v1/agents/model-cards", tags=["agent"])
    async def model_cards(ctn: PlatformContainer = Depends(get_container)) -> dict[str, Any]:
        from aegis.services.model_cards import build_model_cards
        from aegis.services.prompt_registry import PromptRegistry

        cards = build_model_cards(ctn.settings, PromptRegistry.builtin())
        return {
            "cards": cards,
            "measured_columns": [],
            "llm_configured": ctn.llm_gateway is not None and ctn.llm_gateway.available,
            "note": "模型/版本来自真实路由配置与已迁入的版本化 prompt；训练集与评测分未做即 None，不冒充达标。",
        }

    return router
