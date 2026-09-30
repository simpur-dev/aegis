"""边缘侧（站点↔网关弱网链路）POC：subject↔key expression 映射、有界边缘缓冲、Zenoh 传输。

导入本包不会加载 eclipse-zenoh 运行时：zenoh 只在 ZenohEdgeBus.connect() 内惰性导入，
因此 keyexpr 与 offline_buffer 的单元测试可在无 zenoh 环境下运行。
"""

from __future__ import annotations

from aegis.edge.errors import (
    EdgeQueryError,
    KeyExprMappingError,
    ZenohOperationError,
    ZenohUnavailableError,
)
from aegis.edge.keyexpr import (
    decode_chunk,
    encode_chunk,
    is_pattern,
    keyexpr_to_pattern,
    keyexpr_to_subject,
    pattern_to_keyexpr,
    subject_to_keyexpr,
)
from aegis.edge.offline_buffer import BufferedSample, BufferStats, EdgeOfflineBuffer, PutResult
from aegis.edge.poc_report import LatencyRow, build_report, latency_row, percentile, verdict, weak_network_summary
from aegis.edge.zenoh_transport import ZenohEdgeBus, ZenohLinkSettings, aegis_priority_to_zenoh_name

__all__ = [
    "BufferStats",
    "BufferedSample",
    "EdgeOfflineBuffer",
    "EdgeQueryError",
    "KeyExprMappingError",
    "LatencyRow",
    "PutResult",
    "ZenohEdgeBus",
    "ZenohLinkSettings",
    "ZenohOperationError",
    "ZenohUnavailableError",
    "aegis_priority_to_zenoh_name",
    "build_report",
    "decode_chunk",
    "encode_chunk",
    "is_pattern",
    "keyexpr_to_pattern",
    "keyexpr_to_subject",
    "latency_row",
    "pattern_to_keyexpr",
    "percentile",
    "subject_to_keyexpr",
    "verdict",
    "weak_network_summary",
]
