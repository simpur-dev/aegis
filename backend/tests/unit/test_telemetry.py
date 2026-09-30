"""链路装配测试：无操作降级、幂等生命周期、导出失败只计数不抛异常、依赖缺席仍可运行。"""

from __future__ import annotations

import pytest

from aegis.observability import telemetry


@pytest.fixture(autouse=True)
def _clean_telemetry_state():
    """每个用例后关停并清零进程内计数，避免模块级单例在用例间串味。"""
    yield
    telemetry.shutdown_telemetry()
    telemetry._spans_dropped = 0


class _RaisingExporter:
    """模拟弱网/端点挂死：导出线程里抛异常的导出器。"""

    def __init__(self) -> None:
        self.calls = 0

    def export(self, spans):
        self.calls += 1
        raise ConnectionError("网络中断：无法连接 OTLP 端点")

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        return None


class _FailingResultExporter:
    """模拟返回非 SUCCESS 的导出器（OTLP 端点 5xx 的真实形态）。"""

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True

    def shutdown(self) -> None:
        return None


class TestNoOpMode:
    def test_init_without_endpoint_returns_false_and_stays_local(self) -> None:
        exporting = telemetry.init_telemetry(deployment_environment="test", otlp_endpoint="")
        assert exporting is False
        assert telemetry.is_telemetry_exporting() is False

    def test_tracer_is_usable_before_init(self) -> None:
        tracer = telemetry.get_tracer()
        with tracer.start_as_current_span("未装配前的跨度") as span:
            span.set_attribute("aegis.metric", "x")
        assert tracer is telemetry.get_tracer()

    def test_local_mode_span_is_recording_but_never_exported(self) -> None:
        telemetry.init_telemetry(deployment_environment="test", otlp_endpoint="")
        span = telemetry.get_tracer().start_span("本地记录跨度")
        assert span.is_recording() is True
        span.end()

    def test_missing_dependency_degrades_to_null_tracer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(telemetry, "_OTEL_AVAILABLE", False)
        assert telemetry.init_telemetry(otlp_endpoint="http://127.0.0.1:4318") is False
        with telemetry.get_tracer().start_as_current_span("无操作跨度") as span:
            span.set_attribute("aegis.metric", "x")
            span.set_status("anything")
            assert span.is_recording() is False


class TestEndpointResolution:
    def test_none_looks_up_standard_env_in_signal_priority_order(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318")
        assert telemetry._resolve_endpoint(None) == "http://jaeger:4318/v1/traces"
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://jaeger:4318/v1/traces")
        assert telemetry._resolve_endpoint(None) == "http://jaeger:4318/v1/traces"

    def test_explicit_empty_string_disables_even_with_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4318")
        assert telemetry._resolve_endpoint("") == ""

    def test_traces_path_is_appended_only_when_absent(self) -> None:
        assert telemetry._resolve_endpoint("http://127.0.0.1:4318") == "http://127.0.0.1:4318/v1/traces"
        assert telemetry._resolve_endpoint("http://127.0.0.1:4318/v1/traces") == "http://127.0.0.1:4318/v1/traces"

    def test_non_http_scheme_is_rejected_not_used(self) -> None:
        assert telemetry._resolve_endpoint("dns:///jaeger:4317") == ""

    def test_endpoint_present_enables_export(self) -> None:
        assert telemetry.init_telemetry(otlp_endpoint="http://127.0.0.1:4318") is True
        assert telemetry.is_telemetry_exporting() is True


class TestLifecycle:
    def test_double_init_is_idempotent(self) -> None:
        first = telemetry.init_telemetry(deployment_environment="test", otlp_endpoint="")
        tracer_before = telemetry.get_tracer()
        assert telemetry.init_telemetry(deployment_environment="test", otlp_endpoint="http://127.0.0.1:4318") is first
        assert telemetry.get_tracer() is tracer_before, "重复装配不得替换 provider，避免跨度串台"

    def test_shutdown_is_idempotent_and_safe_without_init(self) -> None:
        telemetry.shutdown_telemetry()
        telemetry.shutdown_telemetry()
        telemetry.init_telemetry(deployment_environment="test", otlp_endpoint="")
        telemetry.shutdown_telemetry()
        telemetry.shutdown_telemetry()
        assert telemetry.is_telemetry_exporting() is False

    def test_reinit_after_shutdown_produces_working_tracer(self) -> None:
        telemetry.init_telemetry(deployment_environment="test", otlp_endpoint="")
        telemetry.shutdown_telemetry()
        telemetry.init_telemetry(service_name="aegis-retry", deployment_environment="test", otlp_endpoint="")
        span = telemetry.get_tracer().start_span("重装配后的跨度")
        assert span.is_recording() is True
        span.end()

    def test_resource_attributes_carry_service_and_environment(self) -> None:
        telemetry.init_telemetry(service_name="aegis-backend", service_version="9.9.9", deployment_environment="prod", otlp_endpoint="")
        resource = telemetry._state.provider.resource  # type: ignore[union-attr]
        assert resource.attributes["service.name"] == "aegis-backend"
        assert resource.attributes["service.version"] == "9.9.9"
        assert resource.attributes["deployment.environment.name"] == "prod"

    def test_service_version_falls_back_when_package_not_installed(self) -> None:
        telemetry.init_telemetry(service_version=None, deployment_environment="test", otlp_endpoint="")
        assert telemetry._state.provider.resource.attributes["service.version"]  # type: ignore[union-attr]


class TestFailureDegradation:
    def test_raising_exporter_is_counted_and_returns_failure(self) -> None:
        from opentelemetry.sdk.trace.export import SpanExportResult

        inner = _RaisingExporter()
        wrapper = telemetry._CountingSpanExporter(inner)
        spans = ["fake-span"]

        result = wrapper.export(spans)  # type: ignore[arg-type]

        assert result is SpanExportResult.FAILURE
        assert inner.calls == 1
        assert telemetry.dropped_span_count() == 1, "失败必须转成计数而不是异常"

    def test_non_success_result_is_counted(self) -> None:
        wrapper = telemetry._CountingSpanExporter(_FailingResultExporter())
        before = telemetry.dropped_span_count()
        wrapper.export(["s1", "s2"])  # type: ignore[arg-type]
        assert telemetry.dropped_span_count() - before == 2

    async def test_unreachable_endpoint_cannot_break_business_call(self) -> None:
        """端到端降级：指向必然拒绝连接的端口，业务返回值与异常语义都不受遥测影响。"""
        telemetry.init_telemetry(deployment_environment="test", otlp_endpoint="http://127.0.0.1:1/v1/traces")
        from aegis.observability.instrumentation import span_stage
        from aegis.observability.tracer import Tracer

        ledger = Tracer()

        async def business() -> str:
            async with span_stage("collab_txn", 1_000, tracer=ledger, trace_id="trc_" + "a" * 16):
                return "结论已产出"

        assert await business() == "结论已产出"
        telemetry.shutdown_telemetry()  # 关停时强制刷批 → 真实触发一次失败导出
        assert telemetry.dropped_span_count() > 0

    def test_shutdown_swallows_provider_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        telemetry.init_telemetry(deployment_environment="test", otlp_endpoint="")

        class _Broken:
            def force_flush(self, timeout_millis: int) -> bool:
                raise RuntimeError("provider 已损坏")

        telemetry._state.provider = _Broken()  # type: ignore[union-attr]
        telemetry.shutdown_telemetry()
        assert telemetry._state is None
