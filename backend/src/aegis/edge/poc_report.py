"""POC 量测汇总：把时延样本与弱网断言折成一份可核对的比较字典（纯逻辑，不含 I/O）。

口径固定为"同进程、同锁步测法、同契约信封尺寸"，因此 localhost 结论只能用于
"传输层额外开销是否可接受"，不能用于"高原弱网是否可用"——后者由 verdict 显式降级。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from statistics import fmean

# 契约信封尺寸：小 = AgentMessage 事件（telemetry 上报量级），大 = 含上下文快照的 STU。
SMALL_ENVELOPE_BYTES = 300
LARGE_ENVELOPE_BYTES = 20_000

LOCALHOST_CAVEAT = "量测在 localhost 单进程内完成，不构成高原弱网结论"


@dataclass(frozen=True, slots=True)
class LatencyRow:
    transport: str
    pattern: str
    payload_bytes: int
    n: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    min_ms: float
    max_ms: float
    note: str = ""


def percentile(values: Sequence[float], pct: float) -> float:
    """线性插值分位数（与 numpy 默认口径一致）：空序列返回 0，避免报告出现 NaN。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 4)
    position = (len(ordered) - 1) * (pct / 100)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 4)


def latency_row(
    transport: str,
    samples_ms: Sequence[float],
    *,
    pattern: str,
    payload_bytes: int,
    note: str = LOCALHOST_CAVEAT,
) -> LatencyRow:
    return LatencyRow(
        transport=transport,
        pattern=pattern,
        payload_bytes=payload_bytes,
        n=len(samples_ms),
        p50_ms=percentile(samples_ms, 50),
        p95_ms=percentile(samples_ms, 95),
        p99_ms=percentile(samples_ms, 99),
        mean_ms=round(fmean(samples_ms), 4) if samples_ms else 0.0,
        min_ms=round(min(samples_ms), 4) if samples_ms else 0.0,
        max_ms=round(max(samples_ms), 4) if samples_ms else 0.0,
        note=note,
    )


def weak_network_summary(
    *,
    offered: int,
    received_during_outage: int,
    buffered: int,
    replayed: int,
    duplicates: int,
    out_of_order: int,
    lost: int,
    buffer_survived_restart: bool,
    edge_query_supported: bool,
) -> dict[str, object]:
    """弱网证据表：received_during_outage 是 zenoh 原生行为的负向对照（期望为 0）。"""
    return {
        "offered": offered,
        "received_during_outage": received_during_outage,
        "buffered_at_edge": buffered,
        "replayed_after_reconnect": replayed,
        "duplicates": duplicates,
        "out_of_order": out_of_order,
        "lost": lost,
        "exactly_once_in_order": duplicates == 0 and out_of_order == 0 and lost == 0,
        "buffer_survived_session_restart": buffer_survived_restart,
        "edge_query_supported": edge_query_supported,
        "native_store_and_forward_without_edge_buffer": received_during_outage > 0,
    }


def ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def build_report(
    *,
    rows: Sequence[LatencyRow],
    weak_network: Mapping[str, object],
    environment: Mapping[str, object],
    plateau_link_measured: bool = False,
) -> dict[str, object]:
    baseline = {row.pattern: row for row in rows if row.transport == "memory"}
    comparisons: list[dict[str, object]] = []
    for row in rows:
        reference = baseline.get(row.pattern)
        entry: dict[str, object] = asdict(row)
        if reference is not None and row.transport != "memory":
            entry["vs_memory_p50"] = ratio(row.p50_ms, reference.p50_ms)
            entry["vs_memory_p95"] = ratio(row.p95_ms, reference.p95_ms)
        comparisons.append(entry)
    report: dict[str, object] = {
        "environment": dict(environment),
        "latency_rows": [asdict(row) for row in rows],
        "latency_comparison": comparisons,
        "weak_network": dict(weak_network),
        "verdict": verdict(weak_network=weak_network, plateau_link_measured=plateau_link_measured),
        "caveats": [
            LOCALHOST_CAVEAT,
            "zenoh 侧为同进程双会话（peer↔peer, tcp/127.0.0.1），非两节点双机",
            "内存总线侧为单对象回环，无跨进程成本，故其绝对值只作下界参考",
        ],
    }
    return report


def verdict(*, weak_network: Mapping[str, object], plateau_link_measured: bool = False) -> dict[str, object]:
    """站点↔网关角色的可核对结论：本地只证语义，不证链路质量。"""
    exactly_once = bool(weak_network.get("exactly_once_in_order"))
    survived = bool(weak_network.get("buffer_survived_session_restart"))
    reasons: list[str] = []
    if not plateau_link_measured:
        reasons.append("未在高衰减/高 RTT/丢包链路上量测：本地 localhost 无法复现高原弱网条件")
    if not exactly_once:
        reasons.append("重放未满足 exactly-once + 保序")
    if not survived:
        reasons.append("缓冲区未能跨越会话重启存活")
    overall = "NEEDS FIELD TEST" if not plateau_link_measured else ("ADOPT" if not reasons else "DO-NOT-ADOPT")
    return {
        "role": "station<->gateway weak-network link",
        "overall": overall,
        "reasons": reasons,
        "semantics_proved_locally": {
            "pubsub": True,
            "request_reply": True,
            "edge_store_query": bool(weak_network.get("edge_query_supported")),
            "buffered_replay_exactly_once": exactly_once,
            "buffer_survives_session_restart": survived,
        },
        "not_provable_locally": [
            "NOT PROVABLE LOCALLY: 卫星/4G 回传的 RTT 抖动与衰减丢包下的重传成本",
            "NOT PROVABLE LOCALLY: 多网关冗余与路由收敛时间",
            "NOT PROVABLE LOCALLY: 长时间断链（小时级）下的边缘存储功耗与介质磨损",
        ],
        "retire_nats": False,
    }


__all__ = [
    "LARGE_ENVELOPE_BYTES",
    "LOCALHOST_CAVEAT",
    "SMALL_ENVELOPE_BYTES",
    "LatencyRow",
    "build_report",
    "latency_row",
    "percentile",
    "ratio",
    "verdict",
    "weak_network_summary",
]
