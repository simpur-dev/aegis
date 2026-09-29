"""LLM 网关（参考 NexusMind `backend/app/utils/llm_client.py` 改造为异步）。

与基线项目的差异是刻意的：
1. 全异步（AsyncOpenAI），不再阻塞 Web 框架的事件循环；
2. 未配置密钥时 `available=False` 而不是抛异常——调用方据此走规则降级；
3. 输出异常（非法 JSON、空白返回、被截断）抛类型化错误，由上层决定降级，不静默兜底。

当前平台侧用途：预警文案润色与藏汉双语翻译（注入 `WarningService` 的 translator）。
研判/拆解的智能部分属于智能体方，不在平台侧调用 LLM，避免职责越界。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from aegis.config import Settings, get_settings
from aegis.errors import AegisError, ErrorCode

log = logging.getLogger("aegis.services.llm")

_FENCE_OPEN = re.compile(r"^```(?:json)?\s*\n?", re.IGNORECASE)
_FENCE_CLOSE = re.compile(r"\n?```\s*$")
_THINK_BLOCK = re.compile(r"<think>[\s\S]*?</think>")


class LlmUnavailableError(AegisError):
    code = ErrorCode.NOT_READY


class MalformedLlmOutputError(AegisError):
    code = ErrorCode.INTERNAL


class LlmGateway:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        client: Any | None = None,
        timeout_seconds: float | None = None,
        max_tokens: int = 2_048,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self._api_key = api_key if api_key is not None else cfg.llm_api_key
        self._base_url = base_url or cfg.llm_base_url
        self._model = model or cfg.llm_model
        self._timeout = timeout_seconds if timeout_seconds is not None else cfg.llm_timeout_seconds
        self._max_tokens = max_tokens
        self._client = client

        if self._client is None and self._api_key:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url, timeout=self._timeout)

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def model(self) -> str:
        return self._model

    async def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.3, max_tokens: int | None = None) -> str:
        if not self.available or self._client is None:
            raise LlmUnavailableError("LLM 未配置（缺少 api key 或 client）")
        client = self._client
        response = await client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens or self._max_tokens,
        )
        content = (response.choices[0].message.content or "").strip()
        # 部分供应商会把思考过程混在正文里，必须清掉再交给下游
        content = _THINK_BLOCK.sub("", content).strip()
        if not content:
            raise MalformedLlmOutputError("LLM 返回空内容", detail={"model": self._model})
        return content

    async def chat_json(self, messages: list[dict[str, str]], *, temperature: float = 0.1) -> dict[str, Any]:
        raw = await self.chat(messages, temperature=temperature, max_tokens=self._max_tokens)
        cleaned = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", raw.strip())).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise MalformedLlmOutputError(
                "LLM 返回的 JSON 无法解析",
                detail={"reason": str(exc), "excerpt": cleaned[:200]},
            ) from exc
        if not isinstance(parsed, dict):
            raise MalformedLlmOutputError("LLM 返回的 JSON 顶层不是对象", detail={"type": type(parsed).__name__})
        return parsed

    async def translate(self, text: str, *, target_language: str = "藏语（卫藏方言，书面语）") -> str:
        """翻译预警正文。失败即抛错，由 WarningService 降级为纯中文并标记待译——不伪造译文。"""
        if not text.strip():
            raise MalformedLlmOutputError("待译文本为空")
        messages = [
            {"role": "system", "content": "你是应急管理的多语翻译，译文必须忠实、简洁、无添加信息，只输出译文本身。"},
            {"role": "user", "content": f"把下面的中文预警翻译成{target_language}：\n{text}"},
        ]
        return (await self.chat(messages, temperature=0.0)).strip()

    async def polish_warning(self, title: str, body: str) -> dict[str, str]:
        """把预警正文压成面向公众的简短表述，输出 JSON：{title, body}。"""
        result = await self.chat_json(
            [
                {"role": "system", "content": "输出 JSON 对象，键为 title 与 body，不得新增事实或等级。"},
                {"role": "user", "content": f"标题：{title}\n正文：{body}\n请改写为面向公众的简明预警文案。"},
            ]
        )
        if "title" not in result or "body" not in result:
            raise MalformedLlmOutputError("润色结果缺少 title/body", detail={"keys": sorted(result)})
        return {"title": str(result["title"])[:120], "body": str(result["body"])[:2_000]}


def build_gateway_if_configured(settings: Settings | None = None) -> LlmGateway | None:
    """配置了密钥才返回实例；否则返回 None，让平台走无 LLM 的确定性路径。"""
    cfg = settings or get_settings()
    if not cfg.llm_api_key:
        return None
    return LlmGateway(settings=cfg)
