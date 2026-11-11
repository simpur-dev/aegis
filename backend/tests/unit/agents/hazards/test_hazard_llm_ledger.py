"""灾种 provider 的台账口径：token 真实、attempt 分轮、双通道回退算"需修复"。

【为什么要有这一组】
台账形状与平台网关共享之后，还差**口径**一致：
  ① token 此前恒 0——成本归因在灾种侧整列是假零；
  ② attempt 此前恒 0——双通道回退（通道一没调工具 → 通道二强制 JSON）的第二次成功
     会被首过率读成"首答就过"，灾种首过率系统性虚高；
  ③ "HTTP 200 但没产出合法对象"此前记 ok——自然语言回答冒充首过。
这一组把三件事钉住，且**不改变节点兜底行为**：解析异常照旧上抛、熔断照旧只认传输失败。

【禁网】桩打在 langchain 父类 `_generate`（传输边界）上，适配器自己的熔断/记账逻辑全真跑。
"""

from __future__ import annotations

import threading
from typing import Any

import httpx2
import openai
import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from aegis.agents.core.breaker import LlmBreaker
from aegis.agents.core.ledger import reset_shared_ledger, shared_ledger
from aegis.agents.core.meta import llm_call_meta
from aegis.agents.hazards.llm import ZhipuCompatChatOpenAI
from aegis.services.llm.quality_eval import structured_first_pass_rate


class Out(BaseModel):
    level: int


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_shared_ledger()
    yield
    reset_shared_ledger()


def _client() -> ZhipuCompatChatOpenAI:
    client = ZhipuCompatChatOpenAI(
        model="glm-4-flash",
        temperature=0,
        timeout=1,
        api_key="test-key",
        base_url="https://example.invalid/v1",
        max_retries=0,
    )
    client._breaker = LlmBreaker(threshold=100, cooldown_seconds=60)
    return client


def _result(message: AIMessage, usage: dict[str, int] | None = None) -> ChatResult:
    return ChatResult(
        generations=[ChatGeneration(message=message)],
        llm_output={"token_usage": usage} if usage is not None else {},
    )


def _tool_call(level: Any = 3) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "Out", "args": {"level": level}, "id": "call_1", "type": "tool_call"}],
    )


def _patch_transport(monkeypatch: pytest.MonkeyPatch, script: list[ChatResult | Exception]) -> dict[str, int]:
    """按调用顺序回放脚本；桩在父类 _generate 上——适配器逻辑（熔断/记账/双通道）全真跑。"""
    calls = {"n": 0}

    def fake_generate(self: Any, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        item = script[calls["n"]]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ChatOpenAI, "_generate", fake_generate)
    return calls


def _rows() -> list[Any]:
    return shared_ledger().records()


# ============================================================
# token：来自响应，不再恒 0
# ============================================================


def test_token用量取自响应usage(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(monkeypatch, [_result(AIMessage(content="摘要"), {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})])
    client = _client()
    with llm_call_meta(task="hazard.manager_report", agent_id="plan.loop01"):
        client.invoke([("user", "hi")])
    (row,) = _rows()
    assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (11, 7, 18)
    assert row.validation == "ok"
    assert row.structured is False, "纯文本润色不是结构化调用"
    assert row.attempt == 0


def test_token缺省时退到消息usage_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    message = AIMessage(content="摘要", usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7})
    _patch_transport(monkeypatch, [_result(message)])
    client = _client()
    with llm_call_meta(task="hazard.manager_report"):
        client.invoke([("user", "hi")])
    (row,) = _rows()
    assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (3, 4, 7)


def test_无用量信息才记零(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(monkeypatch, [_result(AIMessage(content="摘要"))])
    client = _client()
    with llm_call_meta(task="hazard.manager_report"):
        client.invoke([("user", "hi")])
    (row,) = _rows()
    assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (0, 0, 0)


# ============================================================
# 双通道：通道一不合规 = malformed（attempt 0），通道二 = 修复轮（attempt 1）
# ============================================================


def test_通道一成功_单行首过(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(monkeypatch, [_result(_tool_call(3), {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})])
    client = _client()
    with llm_call_meta(task="hazard.risk_explain", structured=True, agent_id="assess.factor01"):
        out = client.with_structured_output(Out).invoke([("user", "hi")])
    assert out.level == 3
    (row,) = _rows()
    assert row.validation == "ok"
    assert row.attempt == 0
    assert row.structured is True
    assert row.total_tokens == 7
    rate, detail = structured_first_pass_rate([r.as_dict() for r in _rows()])
    assert rate == 1.0
    assert detail["首过"] == 1


def test_双通道回退_通道一malformed通道二attempt1(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 口径核心：回退成功不能读成"首答就过"——与网关修复轮同一口径。"""
    _patch_transport(
        monkeypatch,
        [
            _result(AIMessage(content="我用自然语言回答你")),  # 通道一：没调工具
            _result(AIMessage(content='{"level": 5}')),  # 通道二：强制 JSON 成功
        ],
    )
    client = _client()
    with llm_call_meta(task="hazard.risk_explain", structured=True, agent_id="assess.factor01"):
        out = client.with_structured_output(Out).invoke([("user", "hi")])
    assert out.level == 5, "回退行为本身不变：仍然产出合法对象"
    rows = _rows()
    assert len(rows) == 2
    assert rows[0].validation == "malformed", "HTTP 200 但没调工具 ≠ 首过"
    assert rows[0].attempt == 0
    assert rows[1].validation == "ok"
    assert rows[1].attempt == 1, "通道二是修复轮，不是首答"
    rate, detail = structured_first_pass_rate([r.as_dict() for r in rows])
    assert rate == 0.0, "首过率分母只算 attempt=0：这一答是需修复，不是首过"
    assert detail == {"首过": 0, "需修复": 1, "传输失败(不参与)": 0}


def test_通道一工具参数不合schema_标malformed且异常照旧上抛(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(monkeypatch, [_result(_tool_call("不是整数"))])
    client = _client()
    with llm_call_meta(task="hazard.risk_explain", structured=True), pytest.raises(ValidationError):
        client.with_structured_output(Out).invoke([("user", "hi")])
    (row,) = _rows()
    assert row.validation == "malformed"
    assert row.attempt == 0
    assert client._breaker.failures == 0, "语义失败不进熔断（口径不变）"


def test_通道二解析失败_两行都malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_transport(
        monkeypatch,
        [_result(AIMessage(content="自然语言")), _result(AIMessage(content="这根本不是 JSON"))],
    )
    client = _client()
    with llm_call_meta(task="hazard.risk_explain", structured=True), pytest.raises(Exception, match="JSON"):
        client.with_structured_output(Out).invoke([("user", "hi")])
    rows = _rows()
    assert [r.validation for r in rows] == ["malformed", "malformed"]
    assert [r.attempt for r in rows] == [0, 1]
    rate, detail = structured_first_pass_rate([r.as_dict() for r in rows])
    assert rate == 0.0
    assert detail["需修复"] == 1, "attempt=1 的行不进首过率分母"


def test_通道二传输失败_attempt随轮次且不进首过分母(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx2.Request("POST", "https://example.invalid/v1/chat/completions")
    _patch_transport(
        monkeypatch,
        [_result(AIMessage(content="自然语言")), openai.APIConnectionError(request=request)],
    )
    client = _client()
    with llm_call_meta(task="hazard.risk_explain", structured=True), pytest.raises(openai.APIConnectionError):
        client.with_structured_output(Out).invoke([("user", "hi")])
    rows = _rows()
    assert rows[1].validation == "transport_error"
    assert rows[1].attempt == 1
    assert client._breaker.failures == 1, "传输失败照旧进熔断"
    rate, detail = structured_first_pass_rate([r.as_dict() for r in rows])
    assert rate == 0.0
    assert detail["传输失败(不参与)"] == 0, "attempt=1 的传输失败本就不在首过口径里"


def test_熔断打开时不落台账行(monkeypatch: pytest.MonkeyPatch) -> None:
    """与网关同口径：调用之前就抛的不可用，不落行（不是模型输出不合规）。"""
    client = _client()
    client._breaker = LlmBreaker(threshold=1, cooldown_seconds=60)
    client._breaker.note_failure()
    from aegis.agents.core.breaker import LlmUnavailableError

    with llm_call_meta(task="hazard.risk_explain", structured=True), pytest.raises(LlmUnavailableError):
        client.with_structured_output(Out).invoke([("user", "hi")])
    assert _rows() == []


# ============================================================
# 并发：改判认上下文，不认"全局最后一行"
# ============================================================


def test_并发双通道改判不串号(monkeypatch: pytest.MonkeyPatch) -> None:
    """研判在专用线程池里跑：两条腿同时回退时，各自的 malformed 必须落在各自的行上。

    桩里放一道栅栏：两条腿都完成通道一之后才许进通道二——若改判按"全局最后一行"找，
    这里必然改到别人的账。
    """
    barrier = threading.Barrier(2, timeout=10)
    counters: dict[int, int] = {}

    def fake_generate(self: Any, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        tid = threading.get_ident()
        index = counters.get(tid, 0)
        counters[tid] = index + 1
        if index == 0:
            barrier.wait()
            return _result(AIMessage(content="自然语言"))
        return _result(AIMessage(content='{"level": 5}'))

    monkeypatch.setattr(ChatOpenAI, "_generate", fake_generate)

    def worker(agent_id: str) -> None:
        client = _client()
        with llm_call_meta(task="hazard.risk_explain", structured=True, agent_id=agent_id):
            out = client.with_structured_output(Out).invoke([("user", "hi")])
        assert out.level == 5

    agents = ["assess.factor01", "assess.factor02"]
    threads = [threading.Thread(target=worker, args=(agent,)) for agent in agents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert all(not thread.is_alive() for thread in threads), "并发用例不得挂死"

    rows = _rows()
    assert len(rows) == 4
    for agent in agents:
        group = [r for r in rows if r.agent_id == agent]
        assert len(group) == 2
        assert group[0].validation == "malformed" and group[0].attempt == 0
        assert group[1].validation == "ok" and group[1].attempt == 1
