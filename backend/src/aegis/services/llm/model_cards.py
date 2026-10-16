"""模型卡片（§4.5 / §5）：每智能体可出示"是什么模型、怎么训的"。

只从**真实配置**装配（网关 v2 路由 `Settings.llm_models` + `PromptRegistry` 版本号），
不编造任何字段：
- model / prompt_version：来自当前路由与已迁入的版本化提示词；未迁入即如实 None。
- trained_dataset：MC 微调线未做 → 如实 None（"未微调"）。
- eval_score：评测线未跑 → 如实 None（"未评测"），绝不用假数字冒充达标。

评审问"这个智能体什么模型、prompt 什么版本、训过没、几分"时，直接出示本卡片；缺的那几项
是"还没做"的诚实留白，不是藏起来的达标。
"""

from __future__ import annotations

from typing import Any

from aegis.config import Settings
from aegis.services.llm.prompt_registry import PromptRegistry

#: 平台自建的五智能体 + 助手（§3），每个对应一个网关 task key。
CARD_TASKS: tuple[str, ...] = ("perceive", "assess", "plan", "execute", "feedback", "assistant")


def build_model_cards(settings: Settings, prompts: PromptRegistry, *, tasks: tuple[str, ...] = CARD_TASKS) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for task in tasks:
        route = settings.llm_models.get(task)
        model = route.model if route is not None and route.model else settings.llm_model
        cards.append(
            {
                "agent": task,
                "model": model,
                "fallback_model": (route.fallback_model if route is not None else "") or None,
                "prompt_version": prompts.version_for_task(task),  # 未迁入版本化真源即 None
                "trained_dataset": None,  # MC QLoRA 线未做
                "eval_score": None,  # 评测线未跑：不编数
                "measured": False,  # 卡片只陈述配置事实，任何"分数"待评测后回填并置 True
            }
        )
    return cards
