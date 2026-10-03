"""指标出口里"哪条腿、什么口径"必须自证（批次 D1 的分开报口径）。

`latency_report()` 是考核数字的唯一出口，所以它得能说清三件事：
触达数字来自 mock 还是真网关、上报这条腿被受理了几次、阈值用的是哪一版。
少任何一项，报表上那些绿色判定就只是颜色。
"""

from __future__ import annotations

import pytest

from aegis.config import Settings
from aegis.container import create_container


def _container(**overrides: object) -> object:
    settings = Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=False,
        delivery_mode="mock",
        llm_api_key="",
        **overrides,  # type: ignore[arg-type]
    )
    return create_container(settings, with_simulator=False)


def test_mock_形态下触达数字标明是演练口径() -> None:
    report = _container().latency_report()
    assert report["delivery"]["mode"] == "mock"
    assert report["delivery"]["channels"], "通道一条都没装配，触达指标就成了无源之水"
    assert all(row.get("mode") == "mock" for row in report["delivery"]["channels"]), report["delivery"]["channels"]


def test_真实通道形态下逐通道带出发送与失败计数() -> None:
    from aegis.domain.enums import Channel
    from aegis.services.delivery import HttpChannelAdapter

    adapter = HttpChannelAdapter(Channel.SMS, "https://gw.example.com", client=object())
    report = create_container(
        Settings(env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, llm_api_key=""),
        with_simulator=False,
        channels={Channel.SMS: adapter},
    ).latency_report()

    assert report["delivery"]["mode"] == "mock", "配置没改成 http 时，出口不得冒充真实通道形态"
    row = report["delivery"]["channels"][0]
    assert row["channel"] == "sms" and row["sent"] == 0 and row["failed"] == 0
    assert row["target"] == "https://gw.example.com", "出口里只允许脱敏后的 scheme://host"


def test_上报腿与阈值版本同在一份出口里() -> None:
    report = _container().latency_report()
    assert report["reports"] == {"submitted": 0, "measured_by_rule": 0, "review_required": 0, "reviews_opened": 0}
    assert report["rulebook"]["rules"] == 9
    assert report["rulebook"]["source"] == "builtin"
    assert report["rulebook"]["uncalibrated"] == 9, "未标定状态必须随出口一起报出，不能只在 /api/v1/rules 里"


@pytest.mark.asyncio
async def test_语义交互这条腿既被记录也被判() -> None:
    """`assistant_reply_ms` 全平台只有一个记账点（`services/assistant.py`）。

    两头都要钉：出口里有样本（否则 ≤3s 那句话没有可对账的数字）、账本里有预算
    （否则样本永远不产生违约计数，"达标"只是因为没人判过）。本机无 LLM 凭据时这条腿
    显然会在毫秒级以下——正因如此，漏登记预算不会有任何症状，只能靠这条用例顶着。
    """
    container = _container()
    async for _ in container.assistant.respond("查一下 540121 的预警", session_id="legs"):
        pass

    report = container.latency_report()
    sample = report["metrics"].get("assistant_reply_ms")
    assert sample and sample["count"] == 1, "语义交互的耗时没进考核出口"
    # 读 `budget_ms`（账本按指标名现取）而不是 `sla_thresholds`（那张表按设置名别名，
    # 拿它断言就等于再抄一份预算）。
    assert sample["budget_ms"] == 3000, sample

    # 拿一条注定超线的样本验证"判"真的在跑：没登记预算时这一条会静默通过
    container.tracer.record("assistant_reply_ms", 5_000.0)
    assert container.latency_report()["violations"].get("assistant_reply_ms") == 1, "超线样本没被判成违约：这条腿的 ≤3s 只是文档"
