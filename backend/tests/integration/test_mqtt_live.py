"""真实 MQTT broker 的在线验证（默认跳过）。

本机起一个 broker（生产按《重构方案》用 EMQX，这里用同一协议的轻量 broker 做线级验证）：
    docker run -d --name aegis-mosquitto -p 127.0.0.1:1883:1883 \\
        eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf
然后：
    AEGIS_TEST_MQTT_HOST=127.0.0.1 pytest tests/integration/test_mqtt_live.py

验的是替身客户端给不了的三件事：
1. 订阅过滤器 `{prefix}/#` 真能收到站端发的多层 topic；
2. aiomqtt 的消息对象形状（topic.value / payload）与我们的解析假设一致；
3. 断开的 broker 会被退避重连计数，而不是让摄取腿静默装死。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from aegis.connectors.mqtt import AiomqttClient, MqttSource

HOST = os.getenv("AEGIS_TEST_MQTT_HOST", "")
PORT = int(os.getenv("AEGIS_TEST_MQTT_PORT", "1883") or "1883")
PREFIX = "field"

pytestmark = pytest.mark.skipif(not HOST, reason="未设置 AEGIS_TEST_MQTT_HOST，跳过真实 MQTT broker 测试")

if sys.platform == "win32":
    # paho 的 asyncio 支持走 loop.add_reader/add_writer，Windows 默认的 Proactor 循环没有这两个钩子。
    # 生产是 Linux 容器部署，不受影响；本机跑这条腿必须换 Selector 循环。见 connectors/mqtt.py 头注释。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]


async def publish(topic: str, payload: dict[str, object] | bytes, *, qos: int = 1) -> None:
    import aiomqtt

    body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    async with aiomqtt.Client(hostname=HOST, port=PORT) as client:
        await client.publish(topic, body, qos=qos)
        # publish 是异步入队的：不等一下就把连接关掉，QoS1 的消息可能还没到 broker
        await asyncio.sleep(0.2)


class TestLiveMqttIngest:
    async def test_station_messages_become_telemetry_readings(self) -> None:
        source = MqttSource(AiomqttClient(host=HOST, port=PORT, topic_prefix=PREFIX, qos=1), prefix=PREFIX)
        await source.start()
        try:
            await _until(lambda: source.status()["connected"] is True)
            await publish(f"{PREFIX}/RG-540121-01", {"region_code": "540121", "metrics": {"rain_10min": 33.5, "debris_level": 0.42}})
            await publish(f"{PREFIX}/LKS-540221-03", {"region_code": "540221", "metric": "lake_level_m", "value": 4.15})
            await publish(f"{PREFIX}/SNW-540321-01/extra/path", {"region_code": "540321", "metric": "new_snow_cm", "value": 6.0})

            readings = await _collect_at_least(source, 4)

            assert [(r.station_id, r.metric, r.value) for r in readings] == [
                ("LKS-540221-03", "lake_level_m", 4.15),
                ("RG-540121-01", "debris_level", 0.42),
                ("RG-540121-01", "rain_10min", 33.5),
                ("SNW-540321-01", "new_snow_cm", 6.0),
            ]
            assert {r.region_code for r in readings} == {"540121", "540221", "540321"}
            assert all(r.source == "mqtt" for r in readings)
            status = source.status()
            assert status["received_messages"] >= 3 and status["rejected"] == {}
        finally:
            await source.stop()

    async def test_malformed_payload_is_counted_not_dropped_silently(self) -> None:
        source = MqttSource(AiomqttClient(host=HOST, port=PORT, topic_prefix=PREFIX), prefix=PREFIX)
        await source.start()
        try:
            await _until(lambda: source.status()["connected"] is True)
            await publish(f"{PREFIX}/BAD-540101-01", b"{not json")

            await _until(lambda: sum(source.status()["rejected"].values()) >= 1, timeout=10.0)

            status = source.status()
            assert status["received_messages"] >= 1
            assert any("JSON" in str(reason) for reason in status["rejected"]), status["rejected"]
            assert await source.collect() == []
        finally:
            await source.stop()

    async def test_broker_outage_is_reported_as_reconnects_not_silence(self) -> None:
        """连一个没人监听的端口：腿必须自己承认"没连上、在重连"，而不是显示 connected。"""
        dead_port = int(os.getenv("AEGIS_TEST_MQTT_DEAD_PORT", "18831"))
        client = AiomqttClient(host=HOST, port=dead_port, topic_prefix=PREFIX, backoff_base_seconds=0.1, backoff_cap_seconds=0.3)
        source = MqttSource(client, prefix=PREFIX)
        await source.start()
        try:
            await _until(lambda: source.status()["reconnects"] >= 1)

            status = source.status()
            assert status["connected"] is False
            assert status["broker"] == f"{HOST}:{dead_port}"
            assert status["last_error"], "重连必须留下可供排障的最后一手原因"
        finally:
            await source.stop()


async def _until(predicate, *, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{timeout}s 内条件未成立")


async def _collect_at_least(source: MqttSource, count: int, *, timeout: float = 10.0) -> list[object]:
    """QoS1 的三条消息到达时间不等：攒够再断言，避免把时序抖动误读成解析缺陷。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    drained: list[object] = []
    while loop.time() < deadline:
        drained.extend(await source.collect())
        if len(drained) >= count:
            return sorted(drained, key=lambda reading: (reading.station_id, reading.metric))  # type: ignore[attr-defined]
        await asyncio.sleep(0.05)
    raise AssertionError(f"{timeout}s 内只收到 {len(drained)} 条读数，期望至少 {count} 条")
