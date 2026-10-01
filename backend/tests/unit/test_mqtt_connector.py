"""MQTT 推送腿用例：载荷解析、有界缓冲、状态面与 aiomqtt 绑定。

覆盖重点不是"能不能连上 broker"（那是集成用例的事），而是三件容易静默出错的事：
1. 载荷约定的每一条拒绝路径都要有**原因**，且原因可计数——"站端在发、平台没数"必须能分开；
2. 缓冲必须有上界，满了按"丢最旧"处理并计数，不能无界吃内存，也不能悄悄丢；
3. 状态面只说目标地址与计数，凭据一个字都不出去。
"""

from __future__ import annotations

import json
import math
import sys
import types
from typing import Any

import pytest

from aegis.connectors.mqtt import (
    AiomqttClient,
    MqttPayloadError,
    MqttSource,
    readings_from_mqtt,
    station_from_topic,
)

TOPIC = "field/RG-540121-01"


class _FakeClient:
    """替身客户端：把"broker 推消息过来"变成一次显式调用。"""

    def __init__(self, *, status: dict[str, object] | None = None) -> None:
        self.sink: Any = None
        self.start_calls = 0
        self.stop_calls = 0
        self._connected = False
        self._status = status if status is not None else {"broker": "127.0.0.1:1883", "topic_filter": "field/#"}

    async def start(self, sink: Any) -> None:
        self.sink = sink
        self.start_calls += 1
        self._connected = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def status(self) -> dict[str, object]:
        return dict(self._status)

    async def deliver(self, topic: str, payload: bytes) -> None:
        assert self.sink is not None, "还没 start() 就有消息进来：订阅关系不对"
        await self.sink(topic, payload)


def batch(**metrics: Any) -> bytes:
    body = {"region_code": "540121", "observed_at": "2026-10-01T08:00:00Z", "metrics": metrics}
    return json.dumps(body).encode()


class TestTopicAndPayload:
    @pytest.mark.parametrize(
        ("topic", "prefix", "expected"),
        [
            ("field/RG-540121-01", "field", "RG-540121-01"),
            ("field/RG-540121-01/rain", "field", "RG-540121-01"),
            ("/field/RG-540121-01/", "field", "RG-540121-01"),
            ("edge/field/RG-1", "edge/field", "RG-1"),
            ("other/RG-1", "field", ""),
            ("field", "field", ""),
            ("", "field", ""),
        ],
    )
    def test_station_is_the_segment_right_after_the_prefix(self, topic: str, prefix: str, expected: str) -> None:
        assert station_from_topic(topic, prefix) == expected

    def test_batch_payload_becomes_one_reading_per_metric(self) -> None:
        readings = readings_from_mqtt(TOPIC, batch(rain_10min=12.5, debris_level=0.4))

        assert [(r.metric, r.value, r.unit) for r in readings] == [("rain_10min", 12.5, "mm"), ("debris_level", 0.4, "m")]
        assert {r.station_id for r in readings} == {"RG-540121-01"}
        assert {r.region_code for r in readings} == {"540121"}
        assert all(r.source == "mqtt" for r in readings)

    def test_single_metric_payload_is_supported_too(self) -> None:
        payload = json.dumps({"region_code": "540221", "metric": "lake_level_m", "value": "3.15"}).encode()

        readings = readings_from_mqtt("field/LKS-540221-03", payload)

        assert readings[0].value == pytest.approx(3.15)
        assert readings[0].unit == "m"
        assert readings[0].observed_at.endswith("Z"), "观测时刻必须归一到 UTC，否则跨时区站点会算错接入时延"

    def test_unknown_metric_is_kept_with_an_explicit_unit(self) -> None:
        """现场加传感器不该被平台吃掉：单位只能标成显式的 unknown，不能是空串（空串=无量纲）。"""
        readings = readings_from_mqtt(TOPIC, batch(soil_moisture_pct=31.0))

        assert readings[0].unit == "unknown"

    def test_naive_observed_at_is_treated_as_utc(self) -> None:
        payload = json.dumps({"region_code": "540121", "observed_at": "2026-10-01T08:00:00", "metric": "rain_10min", "value": 1}).encode()

        assert readings_from_mqtt(TOPIC, payload)[0].observed_at == "2026-10-01T08:00:00Z"

    @pytest.mark.parametrize(
        ("topic", "payload", "reason"),
        [
            ("field/", b"{}", "topic 不含站点段"),
            ("field/RG-1", b"not json", "载荷不是合法 JSON"),
            ("field/RG-1", b"[1,2]", "载荷顶层必须是对象"),
            ("field/RG-1", b'{"metric":"rain_10min","value":1}', "region_code 缺失"),
            ("field/RG-1", b'{"region_code":"5401","metric":"rain_10min","value":1}', "region_code 缺失"),
            ("field/RG-1", '{"region_code":"540121","observed_at":"昨天下午","metric":"rain_10min","value":1}'.encode(), "observed_at"),
            ("field/RG-1", b'{"region_code":"540121"}', "既没有 metrics"),
            ("field/RG-1", b'{"region_code":"540121","metrics":{"rain_10min":"abc"}}', "没有任何可用的数值观测量"),
            ("field/RG-1", b'{"region_code":"540121","metric":"","value":1}', "metric 必须是非空字符串"),
        ],
    )
    def test_every_rejection_carries_a_reason(self, topic: str, payload: bytes, reason: str) -> None:
        with pytest.raises(MqttPayloadError) as caught:
            readings_from_mqtt(topic, payload)

        assert reason in str(caught.value), f"拒绝原因不够具体: {caught.value}"

    def test_non_finite_values_are_dropped_instead_of_poisoning_thresholds(self) -> None:
        body = {"region_code": "540121", "metrics": {"rain_10min": math.nan, "debris_level": math.inf, "wind_speed_ms": 8.0}}
        encoded = json.dumps(body).replace("NaN", "null").replace("Infinity", "1e999")

        readings = readings_from_mqtt(TOPIC, encoded.encode())

        assert [r.metric for r in readings] == ["wind_speed_ms"], "NaN/Inf 进不了阈值判断，也绝不能变成 0 或 1e999 参与判断"

    def test_negative_readings_are_flagged_except_temperature(self) -> None:
        body = {"region_code": "540121", "metrics": {"rain_10min": -3.0, "air_temperature_c": -12.5}}

        readings = readings_from_mqtt(TOPIC, json.dumps(body).encode())

        assert {r.metric: r.quality_flag for r in readings} == {"rain_10min": "suspect", "air_temperature_c": "ok"}


class TestSourceBuffer:
    async def test_start_and_stop_are_idempotent(self) -> None:
        client = _FakeClient()
        source = MqttSource(client)

        await source.start()
        await source.start()
        await source.stop()
        await source.stop()

        assert (client.start_calls, client.stop_calls) == (1, 1)

    async def test_collect_drains_once(self) -> None:
        client = _FakeClient()
        source = MqttSource(client)
        await source.start()
        await client.deliver(TOPIC, batch(rain_10min=1.0, debris_level=0.3))

        first = await source.collect()
        second = await source.collect()

        assert len(first) == 2 and second == []

    async def test_overflow_drops_oldest_and_counts_it(self) -> None:
        """满了丢最旧并留下计数：无界队列在暴雨期会把平台内存吃光，静默丢弃则让人查不到原因。"""
        client = _FakeClient()
        source = MqttSource(client, buffer_limit=2)
        await source.start()
        for value in range(4):
            await client.deliver(TOPIC, batch(rain_10min=float(value)))

        drained = await source.collect()
        status = source.status()

        assert [r.value for r in drained] == [2.0, 3.0]
        assert status["dropped_overflow"] == 2
        assert status["buffered"] == 0

    async def test_rejections_are_grouped_by_reason(self) -> None:
        client = _FakeClient()
        source = MqttSource(client)
        await source.start()

        await client.deliver(TOPIC, batch(rain_10min=1.0))
        await client.deliver("field/RG-1", b"garbage")
        await client.deliver("field/RG-1", b'{"region_code":"540121"}')
        await source.collect()

        status = source.status()
        assert status["received_messages"] == 3
        assert status["parsed_readings"] == 1
        assert sum(status["rejected"].values()) == 2, f"两条坏载荷要各自按原因入账: {status['rejected']}"

    async def test_status_merges_client_facts_with_local_counters(self) -> None:
        """状态面 = 客户端事实 + 本地计数。缺任何一半都判不了"是站端没发，还是平台没接"。"""
        client = _FakeClient(status={"broker": "10.0.0.9:1883", "topic_filter": "field/#", "reconnects": 2})
        source = MqttSource(client, buffer_limit=3)
        await source.start()
        await client.deliver(TOPIC, batch(rain_10min=1.0))

        status = source.status()

        assert status["broker"] == "10.0.0.9:1883" and status["reconnects"] == 2
        assert status["connected"] is True and status["buffer_limit"] == 3
        assert status["received_messages"] == 1 and status["parsed_readings"] == 1

    @pytest.mark.parametrize("limit", [0, -1])
    def test_buffer_limit_must_be_positive(self, limit: int) -> None:
        with pytest.raises(ValueError, match="buffer_limit"):
            MqttSource(_FakeClient(), buffer_limit=limit)


class TestAiomqttBinding:
    def _install_fake_aiomqtt(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        class _Client:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        monkeypatch.setitem(sys.modules, "aiomqtt", types.SimpleNamespace(Client=_Client))
        return captured

    def test_empty_host_is_a_construction_error(self) -> None:
        with pytest.raises(ValueError, match="主机地址"):
            AiomqttClient(host="")

    def test_client_receives_credentials_and_a_bounded_incoming_queue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = self._install_fake_aiomqtt(monkeypatch)
        client = AiomqttClient(
            host="10.0.0.9", port=1883, username="svc", password="s3cr3t", qos=2, incoming_queue_limit=7, client_id="aegis"
        )

        client._open()

        assert captured["hostname"] == "10.0.0.9" and captured["port"] == 1883
        assert (captured["username"], captured["password"]) == ("svc", "s3cr3t"), "凭据只能出现在连接参数里"
        assert captured["identifier"] == "aegis"
        assert captured["max_queued_incoming_messages"] == 7, "入站队列必须有界：无界队列等于把弱网突发全部换成内存占用"

    def test_status_reports_target_without_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_aiomqtt(monkeypatch)
        client = AiomqttClient(host="10.0.0.9", port=1883, username="svc", password="s3cr3t", topic_prefix="field")

        blob = json.dumps(client.status(), ensure_ascii=False)

        assert client.target == "10.0.0.9:1883"
        assert "s3cr3t" not in blob and "svc" not in blob, f"状态面不得出现凭据: {blob}"
        assert json.loads(blob)["topic_filter"] == "field/#"

    @pytest.mark.parametrize(("declared", "used"), [(-1, 0), (5, 2), (1, 1)])
    def test_qos_is_clamped_to_the_protocol_range(self, declared: int, used: int, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install_fake_aiomqtt(monkeypatch)

        client = AiomqttClient(host="h", qos=declared)

        assert client.status()["qos"] == used
