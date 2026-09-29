"""NATS 接口契约守卫：不连服务器，也能挡住"参数名漂移"这类只有上线才爆的 bug。

做法：对 nats-py 的真实签名做反射断言。若某天库升级改了参数名，
本测试立即失败，而不是等生产环境连上 NATS 才炸。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from aegis.bus.nats_bus import NatsBus


def _params(func: Any) -> set[str]:
    return set(inspect.signature(func).parameters)


class TestNatsApiContract:
    def test_connect_accepts_our_kwargs(self) -> None:
        import nats.aio.client as nats_client

        available = _params(nats_client.Client.connect)
        for used in ("servers", "error_cb", "allow_reconnect", "max_reconnect_attempts"):
            assert used in available, f"nats.connect 不再接受参数 {used}"

    def test_publish_accepts_subject_and_payload(self) -> None:
        from nats.js import JetStreamContext

        available = _params(JetStreamContext.publish)
        assert {"subject", "payload"} <= available

    def test_subscribe_accepts_our_kwargs(self) -> None:
        from nats.js import JetStreamContext

        available = _params(JetStreamContext.subscribe)
        for used in ("subject", "stream", "queue", "durable", "cb", "manual_ack"):
            assert used in available, f"js.subscribe 不再接受参数 {used}（曾用错 durable_name/deliver_group）"

    def test_core_subscribe_accepts_queue_and_cb(self) -> None:
        import nats.aio.client as nats_client

        available = _params(nats_client.Client.subscribe)
        assert {"subject", "queue", "cb"} <= available

    def test_stream_config_fields(self) -> None:
        from nats.js import api as js_api

        fields = set(js_api.StreamConfig.__dataclass_fields__)
        for used in ("name", "subjects", "duplicate_window", "max_msgs"):
            assert used in fields, f"StreamConfig 缺少字段 {used}"

    def test_add_stream_takes_config_positionally(self) -> None:
        from nats.js import JetStreamContext

        parameters = list(inspect.signature(JetStreamContext.add_stream).parameters)
        assert parameters[1] == "config", "add_stream 首参应为 StreamConfig"


class TestStreamRouting:
    @pytest.mark.parametrize(
        ("subject", "expected"),
        [
            ("data.rg01.rain_10min", "AEGIS_DATA"),
            ("agent.assess.in", "AEGIS_AGENT"),
            ("agent.assess.out", "AEGIS_AGENT"),
            ("agent.plan.hb", "AEGIS_AGENT"),
            ("workflow.wfi_0001.started", "AEGIS_WF"),
            ("ops.feedback.status", "AEGIS_OPS"),
            ("platform.alert.1.540121", "AEGIS_OPS"),
            ("reply.gw00000001.inbox", "AEGIS_REPLY"),
            ("other.thing.here", None),
        ],
    )
    def test_stream_for(self, subject: str, expected: str | None) -> None:
        assert NatsBus(stream_prefix="AEGIS").stream_for(subject) == expected

    def test_durable_without_stream_is_refused(self) -> None:
        bus = NatsBus()
        assert bus.stream_for("nobody.owns.this") is None

    async def test_operations_require_connection(self) -> None:
        from aegis.domain.messages import make_event
        from aegis.errors import BusNotReadyError

        bus = NatsBus()
        message = make_event(source="perceive.t01", action="perceive.anomaly", payload={}, trace_id="trc_" + "0" * 16)
        with pytest.raises(BusNotReadyError):
            await bus.publish("data.rg01.rain", message)
