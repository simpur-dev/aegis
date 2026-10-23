"""应用层错误映射：`ValidationError` → 统一错误码，别让 schema 版本问题混进普通校验失败。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aegis.workflow.documents import DOCUMENT_SCHEMA_VERSION, WorkflowDocument
from aegis.workflow.errors import WorkflowAppError, WorkflowErrorCode, from_validation_error


def _document(**overrides: object) -> WorkflowDocument:
    payload = {
        "lineage_id": "wf_" + "a" * 12,
        "name": "泥石流处置",
        "nodes": [{"node_id": "a", "type": "notify", "config": {}}],
        **overrides,
    }
    return WorkflowDocument(**payload)


class TestFromValidationError:
    def test_schema_版本错误单独成码(self) -> None:
        with pytest.raises(ValidationError) as caught:
            _document(schema_version=DOCUMENT_SCHEMA_VERSION + 1)
        error = from_validation_error(caught.value)
        assert error.code is WorkflowErrorCode.INVALID_SCHEMA_VERSION
        assert error.status == 422

    def test_普通校验错误带路径与错误列表(self) -> None:
        with pytest.raises(ValidationError) as caught:
            _document(name="")
        error = from_validation_error(caught.value, prefix="document: ")
        assert error.code is WorkflowErrorCode.VALIDATION_FAILED
        assert "name" in error.message
        assert error.message.startswith("document: ")
        assert error.details["errors"][0]["loc"] == ["name"]

    def test_错误体形状固定(self) -> None:
        error = WorkflowAppError(WorkflowErrorCode.NO_CHANGES, "没有变化", details={"content_hash": "sha256:x"})
        assert error.as_detail() == {
            "code": "no_changes",
            "message": "没有变化",
            "details": {"content_hash": "sha256:x"},
        }
        assert error.status == 409

    def test_可覆盖默认状态码(self) -> None:
        error = WorkflowAppError(WorkflowErrorCode.NOT_FOUND, "没找到", status=410)
        assert error.status == 410
