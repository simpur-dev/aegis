"""轻量跨度埋点助手：把一次协同事务落成"一条可第三方引用的链路"，且不扰动时延量测。

两类助手，口径互不侵犯：
- `span` / `start_span` 等只负责"链路结构"——父子关系、跨进程接续、消息链接、降级与错误状态，
  不计时、不写账本，因此可以在热点外沿随手加一条跨度而不改判定口径；
- `span_stage` 负责"计时 + 考核结论"：样本进既有时延账本，同时把预算/违约写进跨度。
  SLA 判定始终以账本为准，本模块不另立第二套计时口径。

三条硬约束：
1. OpenTelemetry 依赖缺席或未装配 provider 时，所有助手都是空操作（不抛、不阻塞）；
2. 跨度只在"量测窗口之外"创建：计时起点已记录的调用点用 `start_span/end_span` 显式配对，
   避免把建跨度的开销算进 ≤2s/≤3s/≤10s 的样本里；
3. 异常一律"先记状态、再原样抛出"，本模块不改异常类型、不吞异常。

链路接续（契约冻结，不加字段）：`contracts/agent_message.v1.schema.json` 规定
`trace_id` 形如 `^trc_[0-9a-f]{16}$`，不是 W3C 的 32 hex，故按确定性映射把 `trc_` 后的
16 hex 左补零成 32 hex 作为 OTel trace id（见 `aegis.observability.telemetry.w3c_trace_id`）；
`msg_id`（`^msg_[0-9a-f]{16}$`）的 16 hex 恰好等于 W3C span id 宽度，直接用作链接目标
（见 `w3c_span_id_from_msg_id`）。由此 Jaeger 里的 traceID 与我们台账里的 trace_id 可互相反查。
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from aegis.observability import telemetry

try:  # 与 telemetry 同口径：依赖缺席时本模块仍必须可导入可用
    from opentelemetry import trace as _otel_trace

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行镜像是否装齐依赖
    _OTEL_AVAILABLE = False

if TYPE_CHECKING:
    from opentelemetry.trace import Link as OtelLink
    from opentelemetry.trace import Span as OtelSpan

    from aegis.config import Settings
    from aegis.observability.metrics import MetricsExporter
    from aegis.observability.tracer import LatencyLedger, Tracer

log = logging.getLogger("aegis.observability.instrumentation")

CONTRACT_MSG_PREFIX = "msg_"
W3C_SPAN_ID_HEX_LEN = 16

METRIC_ATTR = "aegis.metric"
TRACE_ID_ATTR = "aegis.trace_id"
CONTRACT_SPAN_ID_ATTR = "aegis.contract.span_id"
AGENT_ID_ATTR = "aegis.agent_id"
BUDGET_ATTR = "aegis.sla.budget_ms"
BREACH_ATTR = "aegis.sla.breach"
OVER_MS_ATTR = "aegis.sla.over_ms"
OUTCOME_ATTR = "aegis.sla.outcome"
MSG_ID_ATTR = "aegis.msg_id"
REPLY_TO_MSG_ID_ATTR = "aegis.reply_to.msg_id"
DEGRADED_ATTR = "aegis.degraded"
ERROR_CODE_ATTR = "aegis.error.code"
DEGRADATION_EVENT = "aegis.degraded"

#: 跨度开关：弱网边缘站点或取证窗口之外可以整体关掉建跨度的开销（不关掉账本计时）。
_ENV_SPAN_ENABLED = "AEGIS_OTEL_SPAN_ENABLED"

_lock = threading.Lock()
_hot_counts: dict[str, int] = {}
_spans_enabled = os.environ.get(_ENV_SPAN_ENABLED, "1").strip().lower() not in ("0", "false", "no", "off")

AttrValue = str | int | float | bool


def spans_enabled() -> bool:
    """只读：当前是否会创建真实跨度（关闭时助手退化为空操作）。"""
    return _spans_enabled


def set_spans_enabled(enabled: bool) -> None:
    """运行期开关跨度（幂等判定与账本计时不受影响）。"""
    global _spans_enabled
    _spans_enabled = bool(enabled)


# ---------- 热路径：只做聚合计数，不建跨度 ----------


def count_hot_path(name: str, delta: int = 1) -> int:
    """每读数/每消息级热点的替代埋点：进程内聚合计数，恒定 O(1)、不建跨度、不刷网络。

    返回值是该键累加后的读数（便于断言），无副作用以外的开销。
    """
    with _lock:
        total = _hot_counts.get(name, 0) + max(delta, 0)
        _hot_counts[name] = total
        return total


def hot_path_counts() -> dict[str, int]:
    """只读快照：各热点键的累计计数（导出侧或验收报告引用）。"""
    with _lock:
        return dict(_hot_counts)


def reset_hot_path_counts() -> None:
    """清空聚合计数（测试与量测窗口分界使用）。"""
    with _lock:
        _hot_counts.clear()


# ---------- 契约 ID → W3C ID ----------


def w3c_span_id_from_msg_id(msg_id: str | None) -> str | None:
    """契约 msg_id（`msg_` + 16 hex）→ W3C span id：宽度天然相同，校验后直接取用。

    不合式或全零（W3C 非法 ID）返回 None，调用方据此跳过链接而不是造坏链路。
    """
    return telemetry.to_w3c_hex(msg_id, CONTRACT_MSG_PREFIX, W3C_SPAN_ID_HEX_LEN)


def message_link(msg_id: str | None, trace_id: str | None = None) -> OtelLink | None:
    """把一条契约消息的 msg_id 变成 OTel 链接（causation 证据，不是父子关系）。

    用当前事务的 trace_id 组成远端上下文，使 Jaeger 里能从本跨度直接跳到消息对应的那条链路；
    ID 不合式或依赖缺席时返回 None。
    """
    if not _OTEL_AVAILABLE:
        return None
    span_hex = w3c_span_id_from_msg_id(msg_id)
    trace_hex = telemetry.w3c_trace_id(trace_id) if trace_id else None
    if span_hex is None:
        return None
    context = _otel_trace.SpanContext(
        trace_id=int(trace_hex, 16) if trace_hex else 0,
        span_id=int(span_hex, 16),
        is_remote=True,
    )
    if not context.is_valid:
        return None
    return cast("OtelLink", _otel_trace.Link(context=context))


def _span_kind(name: str | None) -> Any:
    """跨度语义：client=平台发出请求，server=平台受理上行，producer/consumer=发布与订阅。"""
    if not _OTEL_AVAILABLE or not name:
        return None
    return getattr(_otel_trace.SpanKind, name.upper(), None)


def _parent_context(trace_id: str | None, parent_span_id: str | None) -> Any | None:
    """已在活动链路里时沿用当前跨度作父节点；否则用契约 trace_id 接续到同一条链路。"""
    return telemetry.remote_parent(telemetry.w3c_trace_id(trace_id), telemetry.w3c_span_id(parent_span_id))


def _coerce(value: object) -> AttrValue:
    return value if isinstance(value, (str, int, float, bool)) else str(value)


def _attributes(
    name: str,
    trace_id: str | None,
    attrs: Mapping[str, AttrValue],
    *,
    contract_span_id: str | None = None,
    agent_id: str | None = None,
    budget_ms: float | None = None,
) -> dict[str, AttrValue]:
    initial: dict[str, AttrValue] = {METRIC_ATTR: name}
    if trace_id:
        initial[TRACE_ID_ATTR] = trace_id
    if contract_span_id:
        initial[CONTRACT_SPAN_ID_ATTR] = contract_span_id
    if agent_id:
        initial[AGENT_ID_ATTR] = agent_id
    if budget_ms is not None:
        initial[BUDGET_ATTR] = budget_ms
    for key, value in attrs.items():
        initial[key] = _coerce(value)
    return initial


# ---------- 跨度助手（全部对失败静默：遥测不得影响业务） ----------


def add_event(span: OtelSpan, name: str, **attrs: AttrValue) -> None:
    """往跨度追加一个事件（用于"第 N 次尝试失败但后来重试成功"这类不改变终态的事实）。

    只记事件不动状态：跨度状态留给终态判定，避免中间过程把整条链路误染成故障。
    """
    try:
        span.add_event(name, attributes={key: _coerce(value) for key, value in attrs.items()})
    except Exception as exc:
        log.debug("跨度事件写入失败，已忽略", extra={"stage": "otlp", "event": name, "error": f"{type(exc).__name__}: {exc}"})


def set_attributes(span: OtelSpan, span_attrs: Mapping[str, AttrValue] | None = None, **attrs: AttrValue) -> None:
    """往已有跨度补属性；跨度不记录或写入失败都只降级为调试日志。

    带点号的属性名（`aegis.msg_id` 这类）走 `span_attrs` 字典，普通标识符可直接作关键字参数。
    """
    merged: dict[str, AttrValue] = dict(span_attrs or {})
    merged.update(attrs)
    if not merged:
        return
    try:
        span.set_attributes({key: _coerce(value) for key, value in merged.items()})
    except Exception as exc:  # 属性写入绝不能回到业务异常
        log.debug("跨度属性写入失败，已忽略", extra={"stage": "otlp", "error": f"{type(exc).__name__}: {exc}"})


def add_link(span: OtelSpan, msg_id: str | None, trace_id: str | None = None) -> None:
    """跨度已创建后补一条消息链接（响应回来才知道 msg_id 的场合）。"""
    link = message_link(msg_id, trace_id)
    if link is None:
        return
    try:
        span.add_link(link.context, attributes=link.attributes or {})
    except Exception as exc:
        log.debug("跨度链接写入失败，已忽略", extra={"stage": "otlp", "error": f"{type(exc).__name__}: {exc}"})


def start_span(
    name: str,
    *,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
    kind: str | None = None,
    links: Sequence[OtelLink] = (),
    span_attrs: Mapping[str, AttrValue] | None = None,
    **attrs: AttrValue,
) -> OtelSpan:
    """显式起一条跨度（不激活为当前上下文）：用于计时窗口已经开始的调用点。

    点号属性名（`aegis.msg_id` 这类）走 `span_attrs` 字典传，普通标识符可直接作关键字参数。
    调用方必须在终止路径上配对 `end_span`；未装配/已关闭时返回无操作跨度，因此无需分支判断。
    """
    if not _spans_enabled:
        return cast("OtelSpan", telemetry._NULL_SPAN)  # 同包内刻意复用无操作单例
    merged: dict[str, AttrValue] = dict(span_attrs or {})
    merged.update(attrs)
    kwargs: dict[str, Any] = {"attributes": _attributes(name, trace_id, merged)}
    if (parent := _parent_context(trace_id, parent_span_id)) is not None:
        kwargs["context"] = parent
    if (resolved_kind := _span_kind(kind)) is not None:
        kwargs["kind"] = resolved_kind
    if links:
        kwargs["links"] = list(links)
    try:
        return telemetry.get_tracer().start_span(name, **kwargs)
    except Exception as exc:  # 装配竞态/依赖缺席：降级为无跨度而不是让业务失败
        log.debug("跨度创建失败，已忽略", extra={"stage": "otlp", "span": name, "error": f"{type(exc).__name__}: {exc}"})
        return cast("OtelSpan", telemetry._NULL_SPAN)


def end_span(span: OtelSpan) -> None:
    """结束显式跨度：重复调用或未记录跨度都安全。"""
    try:
        span.end()
    except Exception as exc:
        log.debug("跨度结束失败，已忽略", extra={"stage": "otlp", "error": f"{type(exc).__name__}: {exc}"})


def _error_status(description: str) -> Any:
    """ERROR 状态对象；依赖缺席时返回 None（此时跨度必然是无操作的，不必写状态）。"""
    if not _OTEL_AVAILABLE:
        return None
    return _otel_trace.Status(_otel_trace.StatusCode.ERROR, description[:500])


def _ok_status() -> Any:
    if not _OTEL_AVAILABLE:
        return None
    return _otel_trace.Status(_otel_trace.StatusCode.OK)


def record_error(span: OtelSpan, error: BaseException, *, code: str | None = None, **attrs: AttrValue) -> None:
    """显式记录错误：异常事件 + ERROR 状态（描述带类型化错误码，便于第三方直接引用）。

    本函数只记状态，不吞异常——调用方随后原样 `raise`。
    """
    try:
        span.record_exception(error, attributes={key: _coerce(value) for key, value in attrs.items()} or {})
        resolved = code or getattr(error, "code", None) or type(error).__name__
        description = f"{resolved}: {getattr(error, 'message', None) or error}"
        if (status := _error_status(description)) is not None:
            span.set_status(status)
    except Exception as exc:
        log.debug("错误状态写入跨度失败，已忽略", extra={"stage": "otlp", "error": f"{type(exc).__name__}: {exc}"})


def mark_error(span: OtelSpan, description: str, *, code: str | None = None, **attrs: AttrValue) -> None:
    """没有异常对象可记时（错误在调用点就地构造并抛出）也要把 ERROR 落到跨度上。

    与 `record_error` 的区别只有"不带异常栈"；状态与事件口径保持一致，
    这样验收时按 `aegis.error.code` 过滤跨度的结果与台账里的 outcome/error_code 对得上。
    """
    try:
        event_attrs: dict[str, AttrValue] = {"message": description[:500]}
        if code:
            event_attrs[ERROR_CODE_ATTR] = _coerce(code)
        for key, value in attrs.items():
            event_attrs[key] = _coerce(value)
        span.add_event("aegis.error", attributes=event_attrs)
        if (status := _error_status(f"{code or 'error'}: {description}")) is not None:
            span.set_status(status)
    except Exception as exc:
        log.debug("错误状态写入跨度失败，已忽略", extra={"stage": "otlp", "error": f"{type(exc).__name__}: {exc}"})


def mark_degraded(span: OtelSpan, reason: str, *, code: str | None = None, **attrs: AttrValue) -> None:
    """记"降级但链路仍成立"：写事件与属性，状态保持非 ERROR。

    降级不是失败——平台侧规则引擎/本地剧本接管后 SLA 依旧可能达成，
    若把它写成 ERROR 会让整条 Jaeger 链路在验收里显示为故障，与账本判定相互矛盾。
    """
    try:
        event_attrs: dict[str, AttrValue] = {"reason": reason[:500]}
        if code:
            event_attrs[ERROR_CODE_ATTR] = _coerce(code)
        for key, value in attrs.items():
            event_attrs[key] = _coerce(value)
        span.add_event(DEGRADATION_EVENT, attributes=event_attrs)
        span.set_attributes({DEGRADED_ATTR: True})
    except Exception as exc:
        log.debug("降级状态写入跨度失败，已忽略", extra={"stage": "otlp", "error": f"{type(exc).__name__}: {exc}"})


@asynccontextmanager
async def span(
    name: str,
    *,
    trace_id: str | None = None,
    parent_span_id: str | None = None,
    kind: str | None = None,
    links: Sequence[OtelLink] = (),
    span_attrs: Mapping[str, AttrValue] | None = None,
    **attrs: AttrValue,
) -> AsyncIterator[OtelSpan]:
    """把一段业务包进当前链路：正常退出记 OK，异常记 ERROR 后原样抛出原始类型化错误。

    作为异步上下文管理器会激活为当前跨度，因此其中的 `await` 与嵌套调用自动成为子跨度，
    一次事务在 Jaeger 里就是一棵树而不是几条并列记录。
    """
    if not _spans_enabled:
        yield cast("OtelSpan", telemetry._NULL_SPAN)  # 开关关闭即空操作
        return

    merged: dict[str, AttrValue] = dict(span_attrs or {})
    merged.update(attrs)
    kwargs: dict[str, Any] = {
        "attributes": _attributes(name, trace_id, merged),
        # 异常与状态改由本助手显式落笔，避免 SDK 自动写入与我们各写一份、描述还不带错误码
        "record_exception": False,
        "set_status_on_exception": False,
    }
    if (parent := _parent_context(trace_id, parent_span_id)) is not None:
        kwargs["context"] = parent
    if (resolved_kind := _span_kind(kind)) is not None:
        kwargs["kind"] = resolved_kind
    if links:
        kwargs["links"] = list(links)

    tracer = telemetry.get_tracer()
    # 降级范围只圈住"建跨度/进入上下文"：业务异常必须原样上浮。
    # 若把 yield 也包进 try，异常路径会二次 yield 同一个异步生成器 —— 调用方将看到
    # RuntimeError("generator didn't stop after athrow()") 而不是原始类型化错误。
    try:
        span_cm = tracer.start_as_current_span(name, **kwargs)
        active = span_cm.__enter__()
    except Exception as build_exc:  # OTel 依赖缺席 / provider 竞态：空操作，不影响业务
        log.debug("跨度上下文创建失败，业务继续", extra={"stage": "otlp", "span": name, "error": str(build_exc)})
        yield cast("OtelSpan", telemetry._NULL_SPAN)
        return

    failure: BaseException | None = None
    try:
        yield active
    except BaseException as exc:
        failure = exc
        if isinstance(exc, Exception):
            record_error(active, exc)
        else:  # 取消不是业务故障：记事件但把 ERROR 留给真实异常，避免弱网演练误判
            _mark_cancelled(active, exc)
        raise
    else:
        _mark_ok(active)
    finally:
        _exit_span_cm(span_cm, failure)


def _exit_span_cm(span_cm: Any, failure: BaseException | None) -> None:
    """结束跨度并解除上下文激活；OTel 自身退出失败也只降级为调试日志，不外抛。"""
    try:
        if failure is None:
            span_cm.__exit__(None, None, None)
        else:
            span_cm.__exit__(type(failure), failure, failure.__traceback__)
    except Exception as exit_exc:
        log.debug("跨度上下文退出失败，已忽略", extra={"stage": "otlp", "error": str(exit_exc)})


def mark_ok(span: OtelSpan) -> None:
    """显式声明跨度成功（用于 `start_span`/`end_span` 手工配对的调用点）。"""
    _mark_ok(span)


def _mark_ok(span: OtelSpan) -> None:
    try:
        if (status := _ok_status()) is not None:
            span.set_status(status)
    except Exception as exc:
        log.debug("OK 状态写入失败，已忽略", extra={"stage": "otlp", "error": f"{type(exc).__name__}: {exc}"})


def _mark_cancelled(span: OtelSpan, exc: BaseException) -> None:
    try:
        span.add_event("aegis.cancelled", attributes={"reason": f"{type(exc).__name__}"})
    except Exception as log_exc:
        log.debug("取消事件写入失败，已忽略", extra={"stage": "otlp", "error": str(log_exc)})


# ---------- SLA：计时 + 考核结论 + 跨度，三者同源 ----------


def register_sla_budgets(
    ledger: LatencyLedger,
    settings: Settings,
    exporter: MetricsExporter | None = None,
) -> dict[str, float]:
    """把课题6 考核指标登记为账本预算，使违约判定从进程启动即生效（预算单位随指标名后缀）。

    只登记已确认口径的真实指标名：`ingest_end_to_end_seconds` 记录的是秒，其余 `_ms` 记录毫秒；
    返回的字典按各自单位给出预算，同时可写入 Prometheus 的 SLA 阈值面板指标。
    """
    budgets: dict[str, float] = {
        # 多节点数据共享同步时延 ≤3s
        "sync_agent_to_gateway_ms": float(settings.sla_sync_ms),
        # 一次协同事务（发出→收到响应）沿用重调度口径 ≤10s
        "collab_txn": float(settings.sla_reschedule_ms),
        # 常规任务调度响应时延 ≤2s（节点"就绪→开始执行"）
        "workflow_schedule_ms": float(settings.sla_schedule_ms),
        "workflow_node_ms": float(settings.sla_schedule_ms),
        # 异常工况识别与重调度响应时长 ≤10s
        "workflow_reschedule_ms": float(settings.sla_reschedule_ms),
        # 链路分段时延沿用调度口径，与 latency_report() 既有映射保持一致
        "stage_assess_ms": float(settings.sla_schedule_ms),
        "stage_plan_ms": float(settings.sla_schedule_ms),
        # 多源数据接入时延 ≤5 分钟（该指标值为秒，故预算也按秒）
        "ingest_end_to_end_seconds": float(settings.sla_ingest_seconds),
        # 人工上报是第四条接入腿（架构文档 §6.2 场景二）：文本→三路解析→进链路，沿用 ≤5 分钟口径
        "report_intake_seconds": float(settings.sla_ingest_seconds),
        # 预警信息生成时间 ≤3 分钟
        "warning_generation_ms": settings.sla_warning_gen_seconds * 1000.0,
        # 预警信息靶向触达 ≤20 分钟
        "warning_reach_ms": settings.sla_reach_seconds * 1000.0,
    }
    for name, budget_ms in budgets.items():
        ledger.set_budget(name, budget_ms)

    if exporter is not None:
        seconds = {name: (budget / 1000.0 if name.endswith("_ms") else budget) for name, budget in budgets.items()}
        exporter.set_sla_gauges(**seconds)
    return budgets


@asynccontextmanager
async def span_stage(
    name: str,
    budget_ms: float | None = None,
    *,
    tracer: Tracer,
    trace_id: str | None = None,
    span_id: str | None = None,
    agent_id: str | None = None,
    **attrs: AttrValue,
) -> AsyncIterator[OtelSpan]:
    """给一段关键链路计时并开一条同名跨度：预算写进跨度，异常置 ERROR 后原样抛出。

    `budget_ms` 传 None 时取账本已登记的预算；显式传入则同时登记到账本，
    使 `latency_report()` 的违约统计、Prometheus 违约计数与 Jaeger 跨度属性三者同源。
    计时与错误语义复用 `span()`，样本照常进账本，分位数不因异常而失真。
    """
    ledger = tracer.ledger
    if budget_ms is None:
        budget: float | None = ledger.budget_for(name)
    else:
        budget = float(budget_ms)
        ledger.set_budget(name, budget)

    started = time.perf_counter()
    outcome = "ok"
    initial = _attributes(name, trace_id, attrs, contract_span_id=span_id, agent_id=agent_id, budget_ms=budget)
    async with span(name, trace_id=trace_id, parent_span_id=span_id, span_attrs=initial) as active:
        try:
            yield active
        except BaseException as exc:
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            raise
        finally:
            elapsed_ms = max((time.perf_counter() - started) * 1000.0, 0.0)
            breach = budget is not None and elapsed_ms > budget
            finish: dict[str, AttrValue] = {OUTCOME_ATTR: outcome, BREACH_ATTR: breach}
            if breach and budget is not None:
                finish[OVER_MS_ATTR] = round(elapsed_ms - budget, 3)
            set_attributes(active, finish)
            ledger.record(name, elapsed_ms, trace_id=trace_id, outcome=outcome, agent_id=agent_id)
