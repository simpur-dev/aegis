"""类型化错误：错误码与《智能体接入规范》§5 一一对应，网关与智能体共用。"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    SCHEMA_INVALID = "E_SCHEMA_INVALID"
    UNREGISTERED_ACTION = "E_UNREGISTERED_ACTION"
    TIMEOUT = "E_TIMEOUT"
    NO_CAPABLE_AGENT = "E_NO_CAPABLE_AGENT"
    INTERNAL = "E_INTERNAL"
    DUPLICATE = "E_DUPLICATE"
    NOT_READY = "E_NOT_READY"
    DATA_FORBIDDEN = "E_DATA_FORBIDDEN"


RETRYABLE_DEFAULT: dict[ErrorCode, bool] = {
    ErrorCode.SCHEMA_INVALID: False,
    ErrorCode.UNREGISTERED_ACTION: False,
    ErrorCode.TIMEOUT: True,
    ErrorCode.NO_CAPABLE_AGENT: True,
    ErrorCode.INTERNAL: True,
    ErrorCode.DUPLICATE: False,
    ErrorCode.NOT_READY: True,
    ErrorCode.DATA_FORBIDDEN: False,
}


class AegisError(Exception):
    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(self, message: str, *, detail: dict[str, object] | None = None, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}
        self.retryable = RETRYABLE_DEFAULT[self.code] if retryable is None else retryable

    def to_payload(self) -> dict[str, object]:
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable, "detail": self.detail}


class SchemaInvalidError(AegisError):
    code = ErrorCode.SCHEMA_INVALID


class UnregisteredActionError(AegisError):
    code = ErrorCode.UNREGISTERED_ACTION


class DeadlineExceededError(AegisError):
    code = ErrorCode.TIMEOUT


class NoCapableAgentError(AegisError):
    code = ErrorCode.NO_CAPABLE_AGENT


class DuplicateMessageError(AegisError):
    code = ErrorCode.DUPLICATE


class BusNotReadyError(AegisError):
    code = ErrorCode.NOT_READY
