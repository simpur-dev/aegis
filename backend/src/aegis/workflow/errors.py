"""工作流应用层错误：一个例外类型带 code/status/details，API 只负责映射。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import ValidationError


class WorkflowErrorCode(StrEnum):
    VALIDATION_FAILED = "validation_failed"
    NOT_FOUND = "not_found"
    REVISION_CONFLICT = "revision_conflict"
    NAME_CONFLICT = "name_conflict"
    NO_CHANGES = "no_changes"
    ARCHIVED = "archived"
    PREVIEW_LIMIT = "preview_limit"
    INVALID_SCHEMA_VERSION = "invalid_schema_version"
    DOCUMENT_TOO_LARGE = "document_too_large"
    INTERNAL = "internal"


_DEFAULT_STATUS: dict[WorkflowErrorCode, int] = {
    WorkflowErrorCode.VALIDATION_FAILED: 422,
    WorkflowErrorCode.NOT_FOUND: 404,
    WorkflowErrorCode.REVISION_CONFLICT: 409,
    WorkflowErrorCode.NAME_CONFLICT: 409,
    WorkflowErrorCode.NO_CHANGES: 409,
    WorkflowErrorCode.ARCHIVED: 409,
    WorkflowErrorCode.PREVIEW_LIMIT: 409,
    WorkflowErrorCode.INVALID_SCHEMA_VERSION: 422,
    WorkflowErrorCode.DOCUMENT_TOO_LARGE: 422,
    WorkflowErrorCode.INTERNAL: 500,
}


class WorkflowAppError(Exception):
    def __init__(
        self,
        code: WorkflowErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status = status if status is not None else _DEFAULT_STATUS[code]

    def as_detail(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "details": self.details}


def from_validation_error(exc: ValidationError, *, prefix: str = "") -> WorkflowAppError:
    """把 pydantic 校验异常收成统一错误；`invalid_schema_version` 单独成码。"""
    errors = exc.errors()
    if any(item.get("type") == WorkflowErrorCode.INVALID_SCHEMA_VERSION.value for item in errors):
        return WorkflowAppError(WorkflowErrorCode.INVALID_SCHEMA_VERSION, "文档 schema_version 不受支持")
    first: dict[str, Any] = dict(errors[0]) if errors else {}
    loc = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", exc))
    return WorkflowAppError(
        WorkflowErrorCode.VALIDATION_FAILED,
        f"{prefix}{loc + ': ' if loc else ''}{message}",
        details={
            "errors": [{"loc": list(item.get("loc", ())), "msg": str(item.get("msg", "")), "type": item.get("type", "")} for item in errors]
        },
    )
