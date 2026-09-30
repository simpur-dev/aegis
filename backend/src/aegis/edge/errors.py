"""edge 子系统的类型化错误：错误码复用 aegis.errors 词表，不新增语义。"""

from __future__ import annotations

from aegis.errors import AegisError, ErrorCode


class KeyExprMappingError(AegisError):
    """subject ↔ key expression 映射不可成立或不可逆。"""

    code = ErrorCode.SCHEMA_INVALID


class ZenohUnavailableError(AegisError):
    """eclipse-zenoh 运行时不可用（依赖缺失或会话打开失败）：属于可重试的就绪问题。"""

    code = ErrorCode.NOT_READY


class ZenohOperationError(AegisError):
    """已连接前提下 zenoh 调用失败：包装 ZError，避免调用方依赖第三方异常类型。"""

    code = ErrorCode.INTERNAL


class EdgeQueryError(AegisError):
    """边缘 store/query 语义错误（选择子非法、无应答）。"""

    code = ErrorCode.NO_CAPABLE_AGENT


__all__ = [
    "EdgeQueryError",
    "KeyExprMappingError",
    "ZenohOperationError",
    "ZenohUnavailableError",
]
