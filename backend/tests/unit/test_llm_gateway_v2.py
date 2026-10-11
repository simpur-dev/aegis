"""LLM 网关 v2 测试（§4.1 / D3）：路由 / 结构化 / 熔断 / 流式 / 用量 / Prompt 注册表。

与既有 `test_llm_gateway.py` 分文件放：那份钉住升级前的对外口径（向后兼容），
这份钉新增能力。桩件客户端把每次 `create(**kwargs)` 的入参全记下，
于是"按 task 选了哪个模型、传了什么参数"是要素级可断言的事实，而非声称。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, Field, field_validator

from aegis.config import Settings
from aegis.observability.tracer import Tracer
from aegis.services.llm_gateway import (
    LlmGateway,
    LlmUnavailableError,
    MalformedLlmOutputError,
    build_gateway_if_configured,
)
from aegis.services.prompt_registry import PromptRegistry, PromptTemplate


class FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = FakeMessage(content)


class FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens


class FakeResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [FakeChoice(content)]
        self.usage = FakeUsage(11, 7)


class FakeCompletions:
    def __init__(self, outputs: list[Any]) -> None:
        self._outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._outputs:
            raise AssertionError("桩件输出已用尽")
        output = self._outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        if kwargs.get("stream"):
            return _FakeStream(output)
        return FakeResponse(output)


class FakeChat:
    def __init__(self, completions: FakeCompletions) -> None:
        self.completions = completions


class _FakeDelta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeStreamChoice:
    def __init__(self, content: str | None) -> None:
        self.delta = _FakeDelta(content)


class _FakeStreamChunk:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeStreamChoice(content)]


class _FakeStream:
    def __init__(self, pieces: list[str]) -> None:
        self._pieces = pieces

    def __aiter__(self) -> _FakeStream:
        self._it = iter(self._pieces)
        return self

    async def __anext__(self) -> _FakeStreamChunk:
        nxt = next(self._it, None)
        if nxt is None:
            raise StopAsyncIteration
        return _FakeStreamChunk(nxt)


class FakeClient:
    def __init__(self, *outputs: Any) -> None:
        self.completions = FakeCompletions(list(outputs))
        self.chat = FakeChat(self.completions)


def gw(
    *outputs: Any, settings: Settings | None = None, tracer: Tracer | None = None, prompts: PromptRegistry | None = None
) -> tuple[LlmGateway, FakeCompletions]:
    client = FakeClient(*outputs)
    return (
        LlmGateway(client=client, settings=settings or Settings(env="test"), tracer=tracer, prompts=prompts),
        client.completions,
    )


class Verdict(BaseModel):
    level: int = Field(ge=1, le=5)
    region: str
    confidence: float

    @field_validator("level")
    @classmethod
    def _frozen_enum(cls, v: int) -> int:
        if v not in (1, 2, 3, 4, 5):
            raise ValueError("等级必须落在 1-5 冻结枚举内")
        return v


class TestRouting:
    async def test_task_routes_to_configured_model_and_params(self) -> None:
        g, calls = gw("定级结论")
        out = await g.chat([{"role": "user", "content": "x"}], task="assess")
        assert out == "定级结论"
        assert calls.calls[0]["model"] == "qwen-plus"
        # assess 路由的 temperature=0.1，而不是 chat 的默认 0.3：证明 task 真正生效。
        assert calls.calls[0]["temperature"] == 0.1

    async def test_default_task_inherits_legacy_model_and_temp(self) -> None:
        g, calls = gw("x")
        await g.chat([{"role": "user", "content": "q"}])
        assert calls.calls[0]["model"] == "qwen-plus"  # 继承 llm_model
        assert calls.calls[0]["temperature"] == 0.3
        assert calls.calls[0]["max_tokens"] == 2_048

    async def test_perceive_routes_to_turbo(self) -> None:
        g, calls = gw("线索")
        await g.chat([{"role": "user", "content": "q"}], task="perceive")
        assert calls.calls[0]["model"] == "qwen-turbo"


class TestChatStructured:
    async def test_valid_first_try_with_json_mode(self) -> None:
        g, calls = gw('{"level": 3, "region": "540121", "confidence": 0.72}')
        result = await g.chat_structured([{"role": "user", "content": "研判"}], schema=Verdict, task="assess")
        assert isinstance(result, Verdict)
        assert result.level == 3 and result.region == "540121"
        assert calls.calls[0]["response_format"] == {"type": "json_object"}

    async def test_enum_violation_is_repaired_then_accepted(self) -> None:
        g, calls = gw('{"level": 9, "region": "540121", "confidence": 0.7}', '{"level": 4, "region": "540121", "confidence": 0.7}')
        result = await g.chat_structured([{"role": "user", "content": "研判"}], schema=Verdict)
        assert result.level == 4
        # 两次调用：首轮越界、修复轮成功；修复轮把非法输出原样带回，要求只改格式。
        assert len(calls.calls) == 2
        repair_msgs = calls.calls[1]["messages"]
        assert repair_msgs[1]["role"] == "assistant"
        assert 'level": 9' in repair_msgs[1]["content"]

    async def test_still_invalid_after_one_repair_raises(self) -> None:
        g, calls = gw('{"level": 9}', '{"level": 9}')
        with pytest.raises(MalformedLlmOutputError):
            await g.chat_structured([{"role": "user", "content": "研判"}], schema=Verdict)
        # 纪律：修复只重发一次，绝不无限重试烧钱。
        assert len(calls.calls) == 2

    async def test_non_json_is_malformed(self) -> None:
        g, _ = gw("这不是 JSON", "这不是 JSON")
        with pytest.raises(MalformedLlmOutputError):
            await g.chat_structured([{"role": "user", "content": "研判"}], schema=Verdict)


class TestCircuitBreaker:
    async def test_no_fallback_opens_after_threshold_then_fail_fast(self) -> None:
        g, calls = gw(RuntimeError("5xx"), RuntimeError("5xx"), settings=Settings(env="test", llm_breaker_fail_threshold=2))
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await g.chat([{"role": "user", "content": "x"}], task="perceive")
        assert len(calls.calls) == 2  # 两次都真打了 qwen-turbo
        # 第三次于调用前就熔断快退（perceive 无备用模型）：不再产生新的 create 调用。
        with pytest.raises(LlmUnavailableError):
            await g.chat([{"role": "user", "content": "x"}], task="perceive")
        assert len(calls.calls) == 2

    async def test_fallback_model_serves_when_primary_fails(self) -> None:
        g, calls = gw(RuntimeError("主模型 5xx"), "来自备用模型的结论")
        out = await g.chat([{"role": "user", "content": "x"}], task="assess")
        assert out == "来自备用模型的结论"
        assert [c["model"] for c in calls.calls] == ["qwen-plus", "qwen-max"]


class TestStreaming:
    async def test_stream_yields_pieces_in_order(self) -> None:
        g, calls = gw(["红色", "预警", "请转移"])
        pieces = [p async for p in g.chat_stream([{"role": "user", "content": "文案"}])]
        assert "".join(pieces) == "红色预警请转移"
        assert calls.calls[0]["stream"] is True

    async def test_stream_unavailable_without_client(self) -> None:
        g = LlmGateway(settings=Settings(env="test", llm_api_key=""))
        with pytest.raises(LlmUnavailableError):
            async for _ in g.chat_stream([{"role": "user", "content": "x"}]):
                pass


class TestObservability:
    async def test_latency_and_usage_recorded_per_call(self) -> None:
        tracer = Tracer()
        g, _ = gw("结论", tracer=tracer)
        await g.chat([{"role": "user", "content": "x"}], task="assess", trace_id="trc_0000000000000001", agent_id="assess.a1")
        assert any(s.name == "llm_call_ms" for s in tracer.ledger._samples)
        snap = g.usage_snapshot()
        assert snap["total_calls"] == 1
        row = snap["by_agent_model"][0]
        assert row["agent_id"] == "assess.a1"
        assert row["model"] == "qwen-plus"
        assert row["validation"]["ok"] == 1
        assert row["total_tokens"] == 18  # FakeUsage 11+7

    async def test_empty_output_marked_malformed(self) -> None:
        g, _ = gw("   ")
        with pytest.raises(MalformedLlmOutputError):
            await g.chat([{"role": "user", "content": "x"}])
        assert g.usage_records()[-1].validation == "malformed"


class TestModelInfo:
    def test_model_info_for_task_reflects_route(self) -> None:
        g, _ = gw("x")
        info = g.model_info_for_task("assess")
        assert info["model"] == "qwen-plus"
        assert info["fallback_model"] == "qwen-max"
        # 未登记版本化 prompt 时如实为 None，不臆造版本号。
        assert info["prompt_version"] is None


class TestUnconfigured:
    def test_build_none_without_key(self) -> None:
        assert build_gateway_if_configured(Settings(env="test", llm_api_key="")) is None

    async def test_structured_unavailable_raises(self) -> None:
        g = LlmGateway(settings=Settings(env="test", llm_api_key=""))
        with pytest.raises(LlmUnavailableError):
            await g.chat_structured([{"role": "user", "content": "x"}], schema=Verdict)


class TestPromptRegistry:
    def test_version_for_task_none_when_unregistered(self) -> None:
        assert PromptRegistry.builtin().version_for_task("assess") is None

    def test_build_and_version(self) -> None:
        reg = PromptRegistry([PromptTemplate(task="assess", version="1.2.0", system="研判", user="区域 {region}")])
        assert reg.version_for_task("assess") == "1.2.0"
        msgs, version = reg.build("assess", region="540121")
        assert version == "1.2.0"
        assert msgs == [{"role": "system", "content": "研判"}, {"role": "user", "content": "区域 540121"}]

    def test_build_unregistered_raises(self) -> None:
        with pytest.raises(KeyError):
            PromptRegistry.builtin().build("assess")

    def test_missing_template_var_raises(self) -> None:
        reg = PromptRegistry([PromptTemplate(task="plan", version="1.0.0", system="s", user="{goal} 目标")])
        with pytest.raises(ValueError):
            reg.build("plan")

    def test_from_dir_loads_versioned_files(self, tmp_path) -> None:
        (tmp_path / "assess.json").write_text('{"version": "2.0.1", "system": "定级", "user": "{text}"}', encoding="utf-8")
        reg = PromptRegistry.from_dir(tmp_path)
        assert reg.version_for_task("assess") == "2.0.1"
        assert reg.tasks() == ["assess"]

    def test_from_dir_missing_raises(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            PromptRegistry.from_dir(tmp_path / "nope")
