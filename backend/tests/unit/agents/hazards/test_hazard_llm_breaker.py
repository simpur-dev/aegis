"""外部实现的 LLM 熔断与客户端复用的边界测试。

【为什么要有这一组】
2026-11-10 的性能改造给 ext 研判链路加了两件事：SDK 盲重试关掉、LLM 熔断。
两件都**改变了失败路径的行为**，所以必须有测试钉住"改完还是不是原来那个意思"：
  ① 熔断打开时，节点仍走既有兜底（模板说明 / 规则判断），**结论不丢、字段不减**；
  ② 熔断只对"服务不可用"记账，对"语义错误"（模型返回非法 JSON）**不记账**——
     否则一个可修的提示词问题会被一段冷却期糊过去；
  ③ 半开窗口只放行**一次**试探（并发下不穿透），窗口到期能恢复；
  ④ 客户端复用：同配置同实例（熔断计数才能跨调用累积），换配置不串号。

【禁网】
本组测试不拨号：熔断行为用假的 client 打桩，端点不可达由桩直接抛。
"""

from __future__ import annotations

import importlib
import threading
from typing import Any

import httpx2
import openai
import pytest

from aegis.agents.core.breaker import LlmBreaker, LlmUnavailableError

# ============================================================
# 熔断状态机：纯逻辑，不涉及网络
# ============================================================


def test_初始为closed且放行() -> None:
    breaker = LlmBreaker(threshold=3, cooldown_seconds=60)
    assert breaker.state() == "closed"
    assert breaker.allow() is True


def test_连续失败达阈值后打开() -> None:
    breaker = LlmBreaker(threshold=3, cooldown_seconds=60)
    breaker.note_failure()
    breaker.note_failure()
    assert breaker.state() == "closed", "未达阈值不该打开"
    assert breaker.allow() is True

    breaker.note_failure()
    assert breaker.state() == "open"
    assert breaker.allow() is False, "打开后必须拒绝（否则每次仍要白等超时）"


def test_成功一次即清零计数() -> None:
    """成功会重置连续失败计数：一次偶发抖动不该把熔断推到阈值。"""
    breaker = LlmBreaker(threshold=3, cooldown_seconds=60)
    breaker.note_failure()
    breaker.note_failure()
    breaker.note_success()
    assert breaker.failures == 0
    breaker.note_failure()
    breaker.note_failure()
    assert breaker.state() == "closed", "计数被成功清零后，两次失败不该打开"


def test_冷却窗口到期转半开并放行一次试探() -> None:
    breaker = LlmBreaker(threshold=1, cooldown_seconds=0.0)
    breaker.note_failure()
    assert breaker.state() == "open"

    assert breaker.allow() is True, "窗口到期应放行试探"
    assert breaker.state() == "half_open"


def test_半开只放行一次试探() -> None:
    """恢复瞬间不该被并发打穿：第二个调用必须继续拒绝。"""
    breaker = LlmBreaker(threshold=1, cooldown_seconds=0.0)
    breaker.note_failure()
    assert breaker.allow() is True
    assert breaker.allow() is False, "试探名额只有一个"
    assert breaker.allow() is False


def test_半开试探失败则重新打开并重新计时() -> None:
    clock = _FakeClock()
    breaker = LlmBreaker(threshold=1, cooldown_seconds=60, clock=clock.now)
    breaker.note_failure()
    assert breaker.allow() is False, "窗口没过不能放行"

    clock.advance(61)  # 冷却窗口到期
    assert breaker.allow() is True, "窗口到期应放行试探"

    breaker.note_failure()
    assert breaker.state() == "open", "试探失败必须重新打开"
    assert breaker.allow() is False, "重新计时期间仍要拒绝"

    clock.advance(61)
    assert breaker.allow() is True, "新一轮窗口到期后又能试探"


def test_冷却窗口未到不得放行() -> None:
    """边界：窗口**刚过期一点点**不算过期。"""
    clock = _FakeClock()
    breaker = LlmBreaker(threshold=1, cooldown_seconds=60, clock=clock.now)
    breaker.note_failure()
    clock.advance(59.9)
    assert breaker.allow() is False, "窗口未满不得放行"
    clock.advance(0.2)
    assert breaker.allow() is True


class _FakeClock:
    """可控的单调钟：冷却窗口的边界必须能确定性测出来，不能靠 sleep 赌。"""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def test_半开试探成功则关闭熔断() -> None:
    breaker = LlmBreaker(threshold=1, cooldown_seconds=0.0)
    breaker.note_failure()
    assert breaker.allow() is True
    breaker.note_success()
    assert breaker.state() == "closed"
    assert breaker.failures == 0
    assert breaker.allow() is True


def test_reset归零() -> None:
    breaker = LlmBreaker(threshold=1, cooldown_seconds=60)
    breaker.note_failure()
    breaker.reset()
    assert breaker.state() == "closed"
    assert breaker.failures == 0


def test_并发调用下状态机不崩且计数准确() -> None:
    """研判在专用线程池里跑，计数会被多线程同时碰。"""
    breaker = LlmBreaker(threshold=1000, cooldown_seconds=60)
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        for _ in range(50):
            breaker.note_failure()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert breaker.failures == 400


# ============================================================
# 记账口径：只认"服务不可用"
# ============================================================


def _status_error(code: int) -> openai.APIStatusError:
    request = httpx2.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx2.Response(code, request=request)
    return openai.APIStatusError("boom", response=response, body=None)


@pytest.mark.parametrize(
    ("exc", "should_count"),
    [
        (openai.APIConnectionError(request=httpx2.Request("POST", "https://example.invalid/x")), True),
        (openai.APITimeoutError(request=httpx2.Request("POST", "https://example.invalid/x")), True),
        (_status_error(500), True),
        (_status_error(503), True),
        (_status_error(429), True),
        (_status_error(401), False),  # Key 错：重试一样错，不该拖进冷却期
        (_status_error(404), False),  # 模型名错：同上
        (_status_error(400), False),  # 请求本身有问题
        (ValueError("模型返回的 JSON 不合法"), False),  # 语义错误：提示词问题，不是服务不可用
    ],
)
def test_只对服务不可用记账(exc: Exception, should_count: bool) -> None:
    from aegis.agents.core.breaker import is_transport_failure

    assert is_transport_failure(exc) is should_count


# ============================================================
# 熔断接进 client 之后的行为
# ============================================================


def _make_client(breaker: LlmBreaker) -> Any:
    from aegis.agents.hazards.llm import ZhipuCompatChatOpenAI

    client = ZhipuCompatChatOpenAI(
        model="glm-4-flash",
        temperature=0,
        timeout=1,
        api_key="test-key",
        base_url="https://example.invalid/v1",
        max_retries=0,
    )
    client._breaker = breaker
    return client


def test_熔断打开后不再发起调用(monkeypatch: pytest.MonkeyPatch) -> None:
    """熔断打开后：一次 HTTP 都不发，直接抛"不可用"。

    桩打在 SDK 的 `completions.create` 上——那是"真正发请求"的那一层，
    所以这里验的正是熔断**是否阻止了调用**，而不是重演熔断逻辑本身。
    """
    breaker = LlmBreaker(threshold=2, cooldown_seconds=60)
    client = _make_client(breaker)
    calls = {"n": 0}

    def fake_create(*_args: Any, **_kwargs: Any) -> Any:
        calls["n"] += 1
        raise _status_error(500)

    monkeypatch.setattr(client.root_client.chat.completions, "create", fake_create)

    # 5xx 经 langchain 层转成 openai.APIStatusError 的子类（OpenAIAPIError）
    for _ in range(2):
        with pytest.raises(openai.APIStatusError):
            client.invoke([("user", "hi")])
    assert calls["n"] == 2, "阈值内应当真的发起调用"

    with pytest.raises(LlmUnavailableError):
        client.invoke([("user", "hi")])
    assert calls["n"] == 2, "熔断打开后不得再发起调用"


def test_熔断记的是传输层失败而非语义失败(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型返回了非法 JSON（语义错误）不该打开熔断——那是提示词问题，不是服务不可用。"""

    breaker = LlmBreaker(threshold=2, cooldown_seconds=60)
    client = _make_client(breaker)

    def fake_create(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("模型返回的 JSON 不合法")

    monkeypatch.setattr(client.root_client.chat.completions, "create", fake_create)

    for _ in range(5):
        with pytest.raises(ValueError):
            client.invoke([("user", "hi")])
    assert breaker.failures == 0, "语义失败不该计入熔断计数"
    assert breaker.state() == "closed"


def test_节点在熔断打开时仍走既有兜底且结论不丢() -> None:
    """★ 语义不变的核心断言：LLM 不可用时，研判结论照常产出。

    熔断打开抛的是 `LlmUnavailableError`（不是平台的 `LlmUnavailableError`），
    但节点既有的是 `except Exception` —— 兜底路径与之前完全一致：
    解释段出模板说明、验证段出规则判断、risk_level 仍来自因子引擎。
    """
    from aegis.agents.hazards.risk_agent.main import run_risk_agent

    assess_module = importlib.import_module("aegis.agents.hazards.risk_agent.nodes.risk_assess_node")
    validate_module = importlib.import_module("aegis.agents.hazards.risk_agent.nodes.risk_validate_node")

    class _BreakerOpen:
        """熔断已打开的 client：任何调用都直接抛"不可用"。"""

        def with_structured_output(self, _schema: Any, **_kw: Any) -> Any:
            return self

        def invoke(self, *_args: Any, **_kwargs: Any) -> Any:
            raise LlmUnavailableError("LLM 熔断中（连续失败达阈值）")

    original_assess = assess_module.get_risk_llm
    original_validate = validate_module.get_risk_llm
    assess_module.get_risk_llm = lambda: _BreakerOpen()  # type: ignore[attr-defined]
    validate_module.get_risk_llm = lambda: _BreakerOpen()  # type: ignore[attr-defined]
    try:
        request, evidence = _assess_request()
        response = run_risk_agent(request, evidence)
    finally:
        assess_module.get_risk_llm = original_assess  # type: ignore[attr-defined]
        validate_module.get_risk_llm = original_validate  # type: ignore[attr-defined]

    # 契约响应（AssessResponse）：等级来自因子引擎，结论不因 LLM 不可用而丢。
    assert response.risk_level, "风险等级必须来自因子引擎，不能因 LLM 不可用而丢"
    assert response.hazard_type, "灾种必须来自请求，不能由 LLM 猜"
    assert response.assessment, "因子引擎信封必须随结论给出"
    assert response.assessment.get("provisional_level") or response.assessment.get("factor_scores"), "信封里应有引擎输出"
    assert "模板兜底" in response.rationale, "解释段应走模板兜底说明"
    assert response.confidence > 0, "置信度由确定性策略给出，不该因 LLM 不可用而丢"


def _assess_request() -> tuple[Any, Any]:
    from aegis.domain.assess import AssessEvidence, AssessRequest, FactorObservation
    from aegis.domain.enums import HazardType, RiskLevel

    factors = (
        "catchment_area_km2",
        "channel_gradient",
        "sediment_supply_index",
        "rainfall_mm_h",
        "snow_depth_cm",
        "rain_10min",
    )
    observations = [
        FactorObservation(
            factor=factor,
            unit="mm",
            samples=12,
            min_value=float(10 + index),
            max_value=float(40 + index),
            mean_value=float(20 + site % 37 + index * 3),
            observed_at="2026-11-10T00:00:00Z",
            station_ids=[f"ST{site:04d}"],
            quality="ok",
        )
        for site in range(8)
        for index, factor in enumerate(factors)
    ]
    evidence = AssessEvidence(
        observations=observations,
        region_code="540121",
        window="1h",
        generated_at="2026-11-10T00:00:00Z",
    )
    request = AssessRequest(
        region_code="540121",
        window="1h",
        hazard_type=HazardType.DEBRIS_FLOW,
        candidate_hazards=[HazardType.DEBRIS_FLOW],
        rule_level=RiskLevel.ORANGE,
        evidence=evidence,
    )
    return request, evidence


# ============================================================
# 客户端复用
# ============================================================


def test_同配置返回同一实例() -> None:
    """复用是熔断能生效的前提：不同实例 = 不同熔断账 = 计数永远归零。"""
    from aegis.agents.hazards.llm import get_llm, reset_llm_clients

    reset_llm_clients()
    try:
        first = get_llm()
        second = get_llm()
        assert first is second
    finally:
        reset_llm_clients()


def test_换凭据不串号() -> None:
    """换了 Key 必须建新实例——否则会拿着旧 Key 的连接去发请求。"""
    from aegis.agents.hazards.llm import get_llm, reset_llm_clients

    reset_llm_clients()
    try:
        first = get_llm()
        second = get_llm()
        assert first is second, "同凭据应复用"

        # 凭据指纹进缓存键：换 Key → 新实例。
        # 凭据入口只有一处（`config.HAZARD_LLM_API_KEY`）——直接改它比改 os.environ 更强：
        # 这同时证明了"换凭据"这件事真的只经过 config 那一个入口。
        import aegis.agents.hazards.config as config_module
        import aegis.agents.hazards.llm as llm_module

        assert not hasattr(llm_module, "os"), "证 llm.py 不再自己读环境变量（凭据入口收口到 config）"
        original = config_module.HAZARD_LLM_API_KEY
        config_module.HAZARD_LLM_API_KEY = "another-key-for-test"
        try:
            third = get_llm()
        finally:
            config_module.HAZARD_LLM_API_KEY = original
        assert third is not first, "换凭据必须建新实例"
    finally:
        reset_llm_clients()


def test_缓存键不落明文凭据() -> None:
    """缓存键里只放凭据的指纹——明文 Key 不该出现在任何会被日志带走的结构里。"""
    from aegis.agents.hazards.llm import _client_cache_key, reset_llm_clients

    reset_llm_clients()
    try:
        key = _client_cache_key("glm-4-flash", "https://example.invalid/v1", "super-secret-key", 0)
        assert "super-secret-key" not in repr(key)
        # 不同 Key → 不同指纹
        other = _client_cache_key("glm-4-flash", "https://example.invalid/v1", "another-key", 0)
        assert key != other
    finally:
        reset_llm_clients()


def test_默认关闭SDK盲重试() -> None:
    """盲重试是实测里 81% 的时延来源（上游持续 5xx 时）。"""
    from aegis.agents.hazards.llm import get_llm, reset_llm_clients

    reset_llm_clients()
    try:
        client = get_llm()
        assert client.max_retries == 0
        assert client.root_client.max_retries == 0, "必须真正传到 SDK 客户端上"
    finally:
        reset_llm_clients()
