"""LLM 网关测试：未配置降级、输出净化、JSON 解析、失败传播与预警集成。

用桩件客户端替代真实供应商，确保测试可离线复现，且能构造"坏输出"验证不静默兜底。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.config import Settings
from aegis.domain.enums import HazardType, RiskLevel
from aegis.domain.messages import TriggerHit
from aegis.observability.tracer import Tracer
from aegis.services.llm_gateway import (
    LlmGateway,
    LlmUnavailableError,
    MalformedLlmOutputError,
    build_gateway_if_configured,
)
from aegis.services.risk_engine import RiskVerdict
from aegis.services.warning_service import WarningService


def sample_verdict(level: RiskLevel = RiskLevel.RED, hazard: HazardType = HazardType.DEBRIS_FLOW) -> RiskVerdict:
    return RiskVerdict(
        hazard_type=hazard,
        region_code="540121",
        risk_level=level,
        confidence=0.85,
        rationale="强降雨叠加泥位抬升",
        hits=[TriggerHit(rule_id="R-DEBRIS-RAIN-1", hazard_type=hazard.value, region_code="540121", score=0.9)],
    )


class FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    def __init__(self, outputs: list[Any]) -> None:
        self._outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._outputs:
            raise AssertionError("桩件输出已用尽")
        output = self._outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return FakeResponse(output)


class FakeChat:
    def __init__(self, completions: FakeCompletions) -> None:
        self.completions = completions


class FakeClient:
    def __init__(self, *outputs: Any) -> None:
        self.completions = FakeCompletions(list(outputs))
        self.chat = FakeChat(self.completions)


def gateway(*outputs: Any, settings: Settings | None = None) -> tuple[LlmGateway, FakeCompletions]:
    client = FakeClient(*outputs)
    return LlmGateway(client=client, settings=settings or Settings(env="test")), client.completions


class TestAvailability:
    def test_unconfigured_is_unavailable_not_raising(self) -> None:
        cfg = Settings(env="test", llm_api_key="")
        gw = LlmGateway(settings=cfg)
        assert gw.available is False

    def test_build_returns_none_without_key(self) -> None:
        assert build_gateway_if_configured(Settings(env="test", llm_api_key="")) is None

    async def test_chat_without_client_raises_unavailable(self) -> None:
        gw = LlmGateway(settings=Settings(env="test", llm_api_key=""))
        with pytest.raises(LlmUnavailableError):
            await gw.chat([{"role": "user", "content": "hi"}])


class TestChat:
    async def test_plain_text(self) -> None:
        gw, calls = gateway("  正文  ")
        assert await gw.chat([{"role": "user", "content": "x"}]) == "正文"
        assert calls.calls[0]["temperature"] == 0.3
        assert calls.calls[0]["max_tokens"] == 2_048

    async def test_think_block_stripped(self) -> None:
        gw, _ = gateway("<think>这里是不该出现的思考过程</think>最终结论")
        assert await gw.chat([{"role": "user", "content": "x"}]) == "最终结论"

    async def test_empty_output_is_error_not_silent(self) -> None:
        gw, _ = gateway("   ")
        with pytest.raises(MalformedLlmOutputError, match="空内容"):
            await gw.chat([{"role": "user", "content": "x"}])

    async def test_none_output_is_error(self) -> None:
        gw, _ = gateway(None)
        with pytest.raises(MalformedLlmOutputError):
            await gw.chat([{"role": "user", "content": "x"}])


class TestChatJson:
    async def test_plain_json(self) -> None:
        gw, _ = gateway('{"title": "红色预警", "body": "尽快转移"}')
        assert await gw.chat_json([{"role": "user", "content": "x"}]) == {"title": "红色预警", "body": "尽快转移"}

    async def test_markdown_fence_stripped(self) -> None:
        gw, _ = gateway('```json\n{"a": 1}\n```')
        assert await gw.chat_json([{"role": "user", "content": "x"}]) == {"a": 1}

    async def test_uppercase_fence_stripped(self) -> None:
        gw, _ = gateway('```JSON\n{"a": 1}\n```')
        assert (await gw.chat_json([{"role": "user", "content": "x"}]))["a"] == 1

    async def test_invalid_json_raises_with_excerpt(self) -> None:
        gw, _ = gateway("这不是 JSON")
        with pytest.raises(MalformedLlmOutputError) as info:
            await gw.chat_json([{"role": "user", "content": "x"}])
        assert "excerpt" in info.value.detail

    async def test_json_array_top_level_rejected(self) -> None:
        gw, _ = gateway("[1, 2, 3]")
        with pytest.raises(MalformedLlmOutputError, match="顶层不是对象"):
            await gw.chat_json([{"role": "user", "content": "x"}])

    async def test_transport_error_propagates(self) -> None:
        gw, _ = gateway(RuntimeError("网络不可达"))
        with pytest.raises(RuntimeError):
            await gw.chat([{"role": "user", "content": "x"}])


class TestTranslateAndPolish:
    async def test_translate_returns_text(self) -> None:
        gw, calls = gateway("བོད་ཡིག 译文")
        assert await gw.translate("请尽快转移") == "བོད་ཡིག 译文"
        assert "请尽快转移" in calls.calls[0]["messages"][1]["content"]

    async def test_translate_empty_input_rejected(self) -> None:
        gw, _ = gateway("x")
        with pytest.raises(MalformedLlmOutputError, match="待译文本为空"):
            await gw.translate("   ")

    async def test_polish_requires_both_keys(self) -> None:
        gw, _ = gateway('{"title": "只有标题"}')
        with pytest.raises(MalformedLlmOutputError, match="缺少 title/body"):
            await gw.polish_warning("标题", "正文")

    async def test_polish_truncates_overlong_fields(self) -> None:
        gw, _ = gateway('{"title": "' + "标" * 300 + '", "body": "正文"}')
        result = await gw.polish_warning("标题", "正文")
        assert len(result["title"]) <= 120


class TestWarningServiceIntegration:
    async def test_translator_fills_tibetan_body(self) -> None:
        gw, _ = gateway("བོད་ཡིག")
        service = WarningService(Tracer(), settings=Settings(env="test"), translator=gw.translate)
        draft = await service.generate(sample_verdict())
        assert draft.record.body_bo == "བོད་ཡིག"
        assert draft.translation_pending is False

    async def test_llm_failure_degrades_to_chinese_only(self) -> None:
        gw, _ = gateway(RuntimeError("供应商 5xx"))
        service = WarningService(Tracer(), settings=Settings(env="test"), translator=gw.translate)
        draft = await service.generate(sample_verdict())
        assert draft.record.body_bo is None
        assert draft.translation_pending is True
        assert draft.record.body_zh
