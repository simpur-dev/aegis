"""契约校验器：加载仓库根 `contracts/*.schema.json` 并校验消息与 STU。

为什么是顶层模块而不是 `bus/gateway.py` 的一部分：它是**信封那侧的裁判**，与
`domain/messages.py`（同一批 schema 的 Pydantic 镜像）成对出现——一个负责"长什么样"，
一个负责"这样对不对"。原先它住在总线网关里，于是 `services/task_parser.py` 与
`services/quality_eval.py` 为了做一次 STU 校验得 import 整个总线层，服务层依赖传输层，
方向是反的。它与 `errors.py` 同级：一个零业务依赖的基础模块。

不进 `domain/` 的原因是硬的：domain 的铁律是"不依赖任何 aegis 包"（连延迟导入都不行），
而这里要抛 `aegis.errors.SchemaInvalidError`。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from aegis.errors import SchemaInvalidError

log = logging.getLogger("aegis.contracts")

#: 仓库根的契约目录（backend/src/aegis/contracts.py → parents[3] = 仓库根）。
_DEFAULT_CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "contracts"


class ContractRegistry:
    """加载并缓存契约 Schema；校验错误只取首要路径，便于回执定位。"""

    def __init__(self, contracts_dir: Path | str | None = None) -> None:
        root = Path(contracts_dir or _DEFAULT_CONTRACTS_DIR)
        if not root.is_dir():
            raise FileNotFoundError(f"契约目录不存在: {root}")
        self._root = root
        self._message = _load_validator(root / "agent_message.v1.schema.json")
        self._stu = _load_validator(root / "stu.v1.schema.json")

    @property
    def contracts_dir(self) -> Path:
        return self._root

    def validate_message(self, data: dict[str, Any]) -> None:
        if error := _first_error(self._message, data):
            raise SchemaInvalidError(
                f"消息不合契约: {error.message}",
                detail={"path": error.json_path, "schema": "agent_message.v1"},
            )

    def validate_stu(self, data: dict[str, Any]) -> None:
        if error := _first_error(self._stu, data):
            raise SchemaInvalidError(
                f"STU 不合契约: {error.message}",
                detail={"path": error.json_path, "schema": "stu.v1"},
            )

    def stu_errors(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """批量校验：返回 [{index, path, error}]，全空即合法。"""
        found: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if error := _first_error(self._stu, item):
                found.append({"index": index, "path": error.json_path, "error": error.message})
        return found


def _load_validator(path: Path) -> Draft202012Validator:
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _first_error(validator: Draft202012Validator, data: dict[str, Any]) -> Any | None:
    errors = sorted(validator.iter_errors(data), key=lambda e: e.json_path)
    return errors[0] if errors else None
