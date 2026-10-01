"""链路追踪接线测试：OTLP 装配必须由服务生命周期驱动，且对外可读。

`telemetry.init_telemetry()` 此前只有测试在调用，生产进程从未装配 provider —— 于是"接了
OpenTelemetry + Jaeger"实际等于跨度全留在本地，第三方在 Jaeger 里什么都查不到。
这个文件把三件事钉住：装配点落在 lifespan，事实落在 /api/v1/integrations，
以及对外可见的端点形状里不允许出现采集器凭据。
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import create_container
from aegis.observability import telemetry

ENDPOINT = "http://127.0.0.1:4318"
CREDENTIALED = "http://ingest:otlp-secret-token@127.0.0.1:4318/v1/traces?tls=true"


@pytest.fixture(autouse=True)
def _clean_telemetry_state():
    """模块级单例必须每例复位，否则上一个用例的 provider 会污染下一个的判定。"""
    yield
    telemetry.shutdown_telemetry()
    telemetry._spans_dropped = 0


def tracing_row(container) -> dict[str, object]:
    return next(state.as_dict() for state in container.integration_status() if state.name == "tracing")


class TestTelemetryStatus:
    def test_uninitialized_is_a_fact_not_an_error(self) -> None:
        status = telemetry.telemetry_status()

        assert status["initialized"] is False
        assert status["exporting"] is False
        assert status["endpoint"] == ""

    def test_local_mode_reports_endpoint_empty(self) -> None:
        telemetry.init_telemetry(deployment_environment="test", otlp_endpoint="")
        status = telemetry.telemetry_status()

        assert status["initialized"] is True
        assert status["exporting"] is False
        assert status["endpoint"] == ""

    def test_configured_endpoint_marks_exporting(self) -> None:
        assert telemetry.init_telemetry(deployment_environment="test", otlp_endpoint=ENDPOINT) is True
        status = telemetry.telemetry_status()

        assert status["exporting"] is True
        assert status["endpoint"] == f"{ENDPOINT}/v1/traces"
        assert status["service_name"] == "aegis-backend"
        assert status["dropped_spans"] == 0

    def test_endpoint_comes_from_the_otel_standard_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读官方环境变量而不是自造一个：运维侧已有 OTel 标准配置，再来一套必然漂移。"""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ENDPOINT)
        telemetry.init_telemetry(deployment_environment="test")

        assert telemetry.telemetry_status()["endpoint"] == f"{ENDPOINT}/v1/traces"


class TestTracingLegIsVisible:
    def test_row_says_local_when_nothing_was_initialized(self) -> None:
        ctn = create_container(Settings(env="test", bus_backend="memory", simulator_enabled=False), with_simulator=False)
        row = tracing_row(ctn)

        assert row["enabled"] is False
        assert row["driver"] == "local"

    def test_row_says_otlp_once_exporting(self) -> None:
        telemetry.init_telemetry(deployment_environment="test", otlp_endpoint=ENDPOINT)
        ctn = create_container(Settings(env="test", bus_backend="memory", simulator_enabled=False), with_simulator=False)

        row = tracing_row(ctn)
        assert row["enabled"] is True
        assert row["driver"] == "otlp"
        assert row["detail"]["endpoint"] == f"{ENDPOINT}/v1/traces"

    def test_dropped_spans_show_up_on_the_same_row(self) -> None:
        """导出在丢跨度时必须能从状态接口看出来：只写日志等于对运维不可见。"""
        telemetry.init_telemetry(deployment_environment="test", otlp_endpoint=ENDPOINT)
        telemetry.note_export_failure(7)
        ctn = create_container(Settings(env="test", bus_backend="memory", simulator_enabled=False), with_simulator=False)

        assert tracing_row(ctn)["detail"]["dropped_spans"] == 7


class TestEndpointNeverCarriesCredentials:
    """端点会出现在日志与 /api/v1/integrations：对外只说"跨度去了哪儿"，不公开采集器凭据。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (CREDENTIALED, "http://127.0.0.1:4318/v1/traces"),
            ("http://user@collector.internal:4318/v1/traces", "http://collector.internal:4318/v1/traces"),
            ("http://collector.internal:4318", "http://collector.internal:4318/v1/traces"),
            ("http://[::1]:4318/v1/traces", "http://[::1]:4318/v1/traces"),
        ],
    )
    def test_status_shows_only_host_and_path(self, raw: str, expected: str) -> None:
        telemetry.init_telemetry(deployment_environment="test", otlp_endpoint=raw)

        assert telemetry.telemetry_status()["endpoint"] == expected

    def test_assembly_log_keeps_the_token_out(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="aegis.observability.telemetry"):
            telemetry.init_telemetry(deployment_environment="test", otlp_endpoint=CREDENTIALED)

        assert "otlp-secret-token" not in caplog.text
        assert "ingest:" not in caplog.text

    def test_integrations_row_is_safe_to_serialize(self) -> None:
        """这一行会整份 JSON 出去，凭据一旦进 detail 就等于公开给任何调用方。"""
        telemetry.init_telemetry(deployment_environment="test", otlp_endpoint=CREDENTIALED)
        ctn = create_container(Settings(env="test", bus_backend="memory", simulator_enabled=False), with_simulator=False)

        assert "otlp-secret-token" not in json.dumps(tracing_row(ctn), ensure_ascii=False)

    def test_redaction_is_display_only(self) -> None:
        """导出器必须拿到完整端点：把凭据从请求里剥掉，带鉴权的采集器会再也收不到跨度。"""
        assert telemetry._resolve_endpoint(CREDENTIALED) == CREDENTIALED

    def test_unparseable_endpoint_is_not_echoed_verbatim(self) -> None:
        assert telemetry._public_endpoint("http://collector:abc/v1/traces") == "<无法解析的端点>"


class TestLifespanOwnsTheAssembly:
    def test_startup_inits_and_shutdown_releases(self, settings: Settings) -> None:
        ctn = create_container(settings, with_simulator=False)
        app = create_app(settings, container=ctn)

        with TestClient(app) as client:
            assert telemetry.telemetry_status()["initialized"] is True
            assert client.get("/healthz").status_code == 200

        assert telemetry.telemetry_status()["initialized"] is False, "跨度 provider 必须随进程一起收，不能留给下一个进程"

    def test_env_var_drives_the_producer_path(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", ENDPOINT)
        ctn = create_container(settings, with_simulator=False)
        with TestClient(create_app(settings, container=ctn)):
            assert telemetry.telemetry_status()["exporting"] is True
