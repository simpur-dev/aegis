"""MQTT 腿的装配面：`build_mqtt` 的三种结局，以及它在容器与状态接口上的落点。

这一层的意义在"看得见的降级"：站端一直在 publish，而平台这边没数，
可能的原因有"没开这条腿 / 依赖没装 / broker 连不上 / 载荷全被拒"四种。
四种必须在 `GET /api/v1/integrations` 上长得不一样，否则排障只能靠猜。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from aegis.config import Settings
from aegis.connectors.mqtt import MqttSource
from aegis.container import create_container
from aegis.integrations import build_mqtt


def iot_available() -> bool:
    return importlib.util.find_spec("aiomqtt") is not None


# 与 `test_mcp_tools.py` 的 needs_mcp 同形：没装 extra 的开发机上这几条是必然的红，装了又永远为真，
# 两边都管不住真正会出事的那一刻（CI 或镜像把 `iot` 摘掉 ⇒ 订阅腿静默 absent 而测试照旧绿）。
# 真正管得住的是下面 TestLegInstalledInDeploySurfaces 那条按部署面判定的守卫用例。
needs_iot = pytest.mark.skipif(not iot_available(), reason="未安装可选依赖 aiomqtt（`uv sync --extra iot`）")


def mqtt_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "bus_backend": "memory",
        "simulator_enabled": False,
        "mqtt_enabled": True,
        "mqtt_host": "10.0.0.9",
        "mqtt_password": "s3cr3t",
    }
    base.update(overrides)
    return Settings(**base)


def mqtt_row(container: Any) -> Any:
    return next(state for state in container.integration_status() if state.name == "mqtt")


class TestBuildMqtt:
    def test_disabled_leg_is_absent_from_the_chain(self) -> None:
        source, state = build_mqtt(Settings(env="test", mqtt_enabled=False))

        assert source is None
        assert (state.name, state.enabled, state.driver) == ("mqtt", False, "off")

    def test_missing_dependency_becomes_an_explicit_unavailable_fact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "aiomqtt", None)  # import aiomqtt -> ImportError

        source, state = build_mqtt(mqtt_settings())

        assert source is None
        assert (state.enabled, state.driver) == (False, "mqtt-unavailable")
        assert state.detail["broker"] == "10.0.0.9:1883"
        assert "aiomqtt" in str(state.detail["reason"])

    @needs_iot
    def test_enabled_leg_carries_target_not_credentials(self) -> None:
        source, state = build_mqtt(mqtt_settings())

        assert isinstance(source, MqttSource)
        assert (state.enabled, state.driver) == (True, "mqtt")
        assert state.detail["broker"] == "10.0.0.9:1883"
        assert state.detail["topic_filter"] == "field/#"
        assert "s3cr3t" not in json.dumps(state.detail, ensure_ascii=False)
        assert "mqtt_password" not in json.dumps(state.detail, ensure_ascii=False)

    @needs_iot
    def test_empty_credentials_reach_the_client_as_anonymous(self) -> None:
        source, _state = build_mqtt(mqtt_settings(mqtt_password=""))
        assert source is not None

        client = source._client
        assert client._username is None and client._password is None


class TestLegInstalledInDeploySurfaces:
    def test_ci与镜像都装了这一条iot腿(self) -> None:
        """装没装这腿由部署面说了算，不由本机环境说了算。

        2026-10-13 的实测：`deploy/Dockerfile` 一直带 `--extra iot`，而 CI 那句 `uv sync` 少了它——
        于是这条腿的装配面用例在 CI 上是红的（当时还没有 needs_iot，`isinstance(source, MqttSource)`
        直接 `assert False`），而本机装着 iot 永远绿。更要紧的是它被 `--maxfail=1` 挡在
        `tests/perf` 那条红后面，直到那条修好才露出来：一档门禁的红会盖住下一档的缺口。
        """
        repo = Path(__file__).resolve().parents[3]
        ci = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        dockerfile = (repo / "deploy" / "Dockerfile").read_text(encoding="utf-8")
        assert "--extra iot" in ci, "CI 不装 iot 时 MQTT 用例只是跳过，绿色 CI 测不到订阅腿"
        assert "--extra iot" in dockerfile, "镜像少这个 extra ⇒ 容器里 mqtt 永远 driver=mqtt-unavailable"


@needs_iot
class TestContainerMqttLeg:
    def test_mqtt_source_joins_the_ingest_round(self) -> None:
        ctn = create_container(mqtt_settings(), with_simulator=False)

        assert [source.name for source in ctn.ingest.sources] == ["mqtt"]
        assert ctn.mqtt is not None

    def test_row_reports_runtime_counters(self) -> None:
        ctn = create_container(mqtt_settings(), with_simulator=False)

        row = mqtt_row(ctn)

        assert row.enabled is True and row.driver == "mqtt"
        assert row.detail["buffered"] == 0 and row.detail["received_messages"] == 0
        assert row.detail["connected"] is False, "还没 start()，这一行就必须说没连上"

    async def test_start_failure_does_not_block_the_platform_but_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctn = create_container(mqtt_settings(), with_simulator=False)

        async def boom() -> None:
            raise RuntimeError("broker refused connection")

        monkeypatch.setattr(ctn.mqtt, "start", boom)
        await ctn.start()
        try:
            detail = mqtt_row(ctn).detail
            transport_up = ctn.transport.connected
        finally:
            await ctn.shutdown()

        assert transport_up is True, "MQTT 起不来不能拖垮平台启动"
        assert "broker refused connection" in str(detail["start_error"])

    async def test_shutdown_releases_the_subscription(self) -> None:
        ctn = create_container(mqtt_settings(), with_simulator=False)
        stops: list[str] = []

        class _Client:
            connected = False

            async def start(self, sink: Any) -> None:
                self.sink = sink

            async def stop(self) -> None:
                stops.append("stopped")

            def status(self) -> dict[str, object]:
                return {"broker": "10.0.0.9:1883"}

        ctn.mqtt = MqttSource(_Client())
        await ctn.start()
        await ctn.shutdown()

        assert stops == ["stopped"]
