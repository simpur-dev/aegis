"""OpenTelemetry 链路装配：配了 OTLP 端点就上报，没配就在本地记录，导出失败只计数与告警。

三条硬约束（与项目"离线/弱网优先"原则一致）：
1. OpenTelemetry 依赖缺席时本模块仍可导入并工作（内置无操作实现），遥测永不是硬依赖；
2. 未配置端点时装配"记录但不导出"的 provider——跨度属性照常可断言，零网络开销；
3. 导出失败（连接拒绝、超时、非 2xx）在导出线程内被计数并写告警日志，绝不抛回业务协程。
"""

from __future__ import annotations

import logging
import os
import random
import threading
from contextlib import nullcontext
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

try:  # 遥测是可选能力：SDK/API 缺席时平台必须照常启动
    from opentelemetry import trace as otel_trace

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行镜像是否装齐依赖
    _OTEL_AVAILABLE = False

if TYPE_CHECKING:
    from opentelemetry.context import Context as OtelContext
    from opentelemetry.sdk.trace.export import SpanExporter
    from opentelemetry.trace import Span as OtelSpan
    from opentelemetry.trace import Tracer as OtelTracer

log = logging.getLogger("aegis.observability.telemetry")

DEFAULT_SERVICE_NAME = "aegis-backend"
TRACER_NAME = "aegis"
#: 端点解析顺序：显式入参 > OTel 官方环境变量（信号级 > 通用级）。空字符串表示显式关闭。
ENDPOINT_ENV_KEYS = ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")
_TRACES_PATH = "/v1/traces"

# BatchSpanProcessor 调参说明（弱网优先，全部在独立导出线程生效，不占业务协程时间片）：
# - 队列 4096 条：约 2~3 个抓取周期的跨度余量；队列满时 SDK 直接丢弃新跨度，
#   这是刻意的背压选择——宁可丢证据，也不能让链路阻塞在遥测上。
# - 2000ms 批量间隔：与"状态同步 ≤3s"同量级，保证验收取证时延可控。
# - 单批 256 条：单次 POST 体积约几十 KB，弱网下不易触发网关超时。
# - 导出超时 5s（SDK 默认 10s）：端点挂死时尽快放弃整批，HTTP 与 processor 用同一预算。
_BATCH_MAX_QUEUE_SIZE = 4_096
_BATCH_SCHEDULE_DELAY_MS = 2_000
_BATCH_MAX_EXPORT_BATCH_SIZE = 256
_EXPORT_TIMEOUT_S = 5.0
_SHUTDOWN_FLUSH_TIMEOUT_MS = 3_000

_lock = threading.Lock()
_state: _TelemetryState | None = None
_global_provider_installed = False
_spans_dropped = 0


class _NullSpan:
    """无操作跨度：依赖缺席或未初始化时保持与真实跨度相同的调用面。"""

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        return None

    def set_status(self, status: Any, description: str | None = None) -> None:
        return None

    def record_exception(self, exception: BaseException, attributes: dict[str, Any] | None = None) -> None:
        return None

    def add_event(self, name: str, attributes: dict[str, Any] | None = None, timestamp: int | None = None) -> None:
        return None

    def is_recording(self) -> bool:
        return False

    def end(self, end_time: int | None = None) -> None:
        return None


_NULL_SPAN = _NullSpan()


class _NullTracer:
    """无操作 tracer：与 OTel Tracer 同签名，跨度不记录、不导出。"""

    def start_as_current_span(
        self,
        name: str,
        context: Any | None = None,
        kind: Any | None = None,
        attributes: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        return nullcontext(cast("OtelSpan", _NULL_SPAN))

    def start_span(
        self,
        name: str,
        context: Any | None = None,
        kind: Any | None = None,
        attributes: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        return cast("OtelSpan", _NULL_SPAN)


_NULL_TRACER = _NullTracer()


class _CountingSpanExporter:
    """把真实导出器的失败转成进程内计数与告警日志，绝不向调用线程抛异常。

    鸭子类型实现 `SpanExporter` 协议（export/force_flush/shutdown）：Base 类要等 SDK 到位才存在，
    而本模块必须在依赖缺席时仍可导入，故在挂入 processor 处用 cast 收敛类型。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def export(self, spans: Any) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        count = len(spans) if spans is not None else 0
        try:
            result = self._inner.export(spans)
        except Exception as exc:  # 网络/序列化/端点异常一律就地降级
            note_export_failure(count)
            log.warning("跨度导出异常，已丢弃并计数", extra={"stage": "otlp", "spans": count, "error": f"{type(exc).__name__}: {exc}"})
            return SpanExportResult.FAILURE
        if result != SpanExportResult.SUCCESS:
            note_export_failure(count)
            log.warning("跨度导出未成功，已丢弃并计数", extra={"stage": "otlp", "result": str(result), "spans": count})
        return result

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            return bool(self._inner.force_flush(timeout_millis))
        except Exception as exc:
            log.warning("跨度导出器刷新失败", extra={"stage": "otlp", "error": str(exc)})
            return False

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception as exc:
            log.warning("跨度导出器关停失败", extra={"stage": "otlp", "error": str(exc)})


class _TelemetryState:
    """一次装配的结果快照：provider 由本模块独占生命周期，避免受全局单例限制。"""

    __slots__ = ("endpoint", "provider", "resource_attrs", "tracer")

    def __init__(self, *, provider: Any | None, tracer: Any, endpoint: str, resource_attrs: dict[str, str]) -> None:
        self.provider = provider
        self.tracer = tracer
        self.endpoint = endpoint
        self.resource_attrs = resource_attrs

    @property
    def exporting(self) -> bool:
        return bool(self.endpoint and self.provider is not None)


def _default_service_version() -> str:
    try:
        return version("aegis-backend")
    except PackageNotFoundError:  # 源码直跑（未 pip/uv 安装）时不留空版本，避免资源属性缺项
        return "0.0.0+source"


def _resolve_endpoint(explicit: str | None) -> str:
    """端点解析：显式入参优先；None 时查标准环境变量；只接受 OTLP/HTTP。"""
    if explicit is None:
        explicit = next((os.environ[key] for key in ENDPOINT_ENV_KEYS if os.environ.get(key, "").strip()), "")
    raw = (explicit or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        log.warning("OTLP 端点非 HTTP(S)，遥测降级为本地记录", extra={"stage": "otlp", "endpoint": raw})
        return ""
    if urlparse(raw).path.strip("/"):
        return raw
    return raw.rstrip("/") + _TRACES_PATH


def _resource_attributes(service_name: str, service_version: str, deployment_environment: str) -> dict[str, str]:
    return {
        "service.name": service_name,
        "service.version": service_version,
        "deployment.environment.name": deployment_environment,
    }


def _build_provider(resource_attrs: dict[str, str], endpoint: str) -> Any | None:
    """构造 provider：无 SDK 返回 None；无端点则不挂 processor（记录但不导出）。"""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create(resource_attrs))
    if not endpoint:
        return provider
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    exporter = _CountingSpanExporter(OTLPSpanExporter(endpoint=endpoint, timeout=_EXPORT_TIMEOUT_S))
    provider.add_span_processor(
        BatchSpanProcessor(
            cast("SpanExporter", exporter),
            max_queue_size=_BATCH_MAX_QUEUE_SIZE,
            schedule_delay_millis=_BATCH_SCHEDULE_DELAY_MS,
            max_export_batch_size=_BATCH_MAX_EXPORT_BATCH_SIZE,
            export_timeout_millis=int(_EXPORT_TIMEOUT_S * 1000),
        )
    )
    return provider


def init_telemetry(
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
    service_version: str | None = None,
    deployment_environment: str = "dev",
    otlp_endpoint: str | None = None,
) -> bool:
    """装配链路追踪，返回是否处于上报模式；重复调用幂等（不叠加 provider）。"""
    global _global_provider_installed, _state
    with _lock:
        if _state is not None:
            log.debug("链路追踪已装配，忽略重复调用", extra={"stage": "otlp", "endpoint": _state.endpoint or "-"})
            return _state.exporting

        endpoint = _resolve_endpoint(otlp_endpoint)
        resource_attrs = _resource_attributes(service_name, service_version or _default_service_version(), deployment_environment)
        provider: Any | None = None
        if not _OTEL_AVAILABLE:
            log.warning("OpenTelemetry 依赖缺席，链路追踪降级为无操作模式（业务链路不受影响）", extra={"stage": "otlp"})
        else:
            try:
                provider = _build_provider(resource_attrs, endpoint)
            except Exception as exc:  # 装配失败也不阻止平台启动，只在首次调用处告警
                provider = None
                log.warning("链路追踪装配失败，降级为无操作模式", extra={"stage": "otlp", "error": f"{type(exc).__name__}: {exc}"})

        if provider is not None:
            tracer = provider.get_tracer(TRACER_NAME)
            if not _global_provider_installed:
                # 全局只安装一次：让后续第三方 instrumentation 能加入同一条链路；
                # 关停后重建的新 provider 由本模块私有持有，不再覆盖全局。
                _global_provider_installed = True
                otel_trace.set_tracer_provider(provider)
        else:
            tracer = _NULL_TRACER

        _state = _TelemetryState(provider=provider, tracer=tracer, endpoint=endpoint, resource_attrs=resource_attrs)
        log.info(
            "链路追踪已装配",
            extra={
                "stage": "otlp",
                "service": resource_attrs["service.name"],
                "environment": resource_attrs["deployment.environment.name"],
                "endpoint": endpoint or "本地记录（不上报）",
                "exporting": bool(endpoint and provider is not None),
            },
        )
        return _state.exporting


def shutdown_telemetry(*, flush_timeout_ms: int = _SHUTDOWN_FLUSH_TIMEOUT_MS) -> None:
    """关停并尽力刷出余量跨度；未初始化或重复关停均为空操作，不抛异常。"""
    global _state
    with _lock:
        state, _state = _state, None
    if state is None or state.provider is None:
        return
    try:
        if not state.provider.force_flush(timeout_millis=flush_timeout_ms):
            log.warning("跨度刷新超时，余量跨度可能未导出", extra={"stage": "otlp", "timeout_ms": flush_timeout_ms})
        state.provider.shutdown()
    except Exception as exc:  # 关停路径上的异常必须就地消化
        log.warning("链路追踪关停异常，已忽略", extra={"stage": "otlp", "error": f"{type(exc).__name__}: {exc}"})


def get_tracer(name: str = TRACER_NAME) -> OtelTracer:
    """取tracer：未装配时返回无操作实现，调用方无需分支判断。"""
    state = _state
    if state is None:
        if not _OTEL_AVAILABLE:
            return cast("OtelTracer", _NULL_TRACER)
        return otel_trace.get_tracer(name)
    if name != TRACER_NAME and state.provider is not None:
        return state.provider.get_tracer(name)
    return cast("OtelTracer", state.tracer)


def remote_parent(trace_id_hex: str | None, span_id_hex: str | None = None) -> OtelContext | None:
    """把 W3C 十六进制 ID 造成为远端父上下文，使跨度落在指定 trace 上；不可用时返回 None。

    已在活动链路中时同样返回 None——让 OTel 沿用当前跨度作父节点，
    嵌套阶段因此聚成一棵树而不是并列根（树的 trace id 由最外层那次映射决定）。
    """
    if not _OTEL_AVAILABLE or not trace_id_hex:
        return None
    if otel_trace.get_current_span().get_span_context().is_valid:
        return None
    try:
        trace_id = int(trace_id_hex, 16)
        span_id = int(span_id_hex, 16) if span_id_hex else _random_span_id()
    except ValueError:
        return None
    span_context = otel_trace.SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=otel_trace.TraceFlags(otel_trace.TraceFlags.SAMPLED),
    )
    if not span_context.is_valid:
        return None
    return otel_trace.set_span_in_context(otel_trace.NonRecordingSpan(span_context))


def _random_span_id() -> int:
    return random.getrandbits(64) or 1  # 仅为合成父跨度生成非零 ID，无安全含义


def dropped_span_count() -> int:
    """只读：累计因导出失败而丢弃的跨度数，供 /metrics 与验收取证。"""
    return _spans_dropped


def is_telemetry_exporting() -> bool:
    """只读：当前是否会把跨度发往 OTLP 端点。"""
    state = _state
    return bool(state and state.exporting)


def note_export_failure(spans: int) -> None:
    """记录一次导出失败丢弃的跨度数（导出线程内调用，加锁保证读侧一致）。"""
    global _spans_dropped
    with _lock:
        _spans_dropped += max(spans, 0)


# ---------- 契约 ID -> W3C ID ----------

CONTRACT_TRACE_PREFIX = "trc_"
CONTRACT_SPAN_PREFIX = "spn_"
W3C_TRACE_ID_HEX_LEN = 32
W3C_SPAN_ID_HEX_LEN = 16
_HEX_CHARS = frozenset("0123456789abcdef")


def to_w3c_hex(value: str | None, prefix: str, width: int) -> str | None:
    """带前缀的契约 ID -> W3C hex：宽度不符、含非 hex 字符、全零一律返回 None。

    全零是 W3C/OTel 的非法 ID，宁可让调用方退回随机 trace id，也不造一条坏链路出来污染取证。
    """
    if not value or not value.startswith(prefix):
        return None
    body = value[len(prefix) :]
    if len(body) * 2 != width or any(ch not in _HEX_CHARS for ch in body):
        return None
    if not body.strip("0"):
        return None
    return body.zfill(width)


def w3c_trace_id(contract_trace_id: str | None) -> str | None:
    """契约 trace_id（`trc_` + 16 hex）-> W3C 32 hex OTel trace id；不合式返回 None。"""
    return to_w3c_hex(contract_trace_id, CONTRACT_TRACE_PREFIX, W3C_TRACE_ID_HEX_LEN)


def w3c_span_id(contract_span_id: str | None) -> str | None:
    """契约 span_id（`spn_` + 8 hex）-> W3C 16 hex OTel span id；不合式返回 None。"""
    return to_w3c_hex(contract_span_id, CONTRACT_SPAN_PREFIX, W3C_SPAN_ID_HEX_LEN)
