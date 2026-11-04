"""席位种子导入：给 `operators` 表一个可重复执行的初始化入口。

为什么需要这条命令：迁移不插默认账号（生产不能出现默认管理员），而席位又是写动作的前置；
没有入口就会出现"表建好了、谁都查不到自己"。

口径：
- **先全量校验再写**：任何一行不合法就整份不导入（Pydantic 逐行校验）。
- **upsert 语义**：同一份清单跑多次结果一致，不产生重复席位。
- **不碰内存视图**：席位必须持久，内存形态没有这条腿。

用法：
    uv run python -m scripts.seed_operators --file operators.json
    uv run python -m scripts.seed_operators --file operators.json --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from aegis.domain.operator import Operator
from aegis.persistence.postgres import PostgresStore
from aegis.services.operators import OperatorService


def _load(path: Path) -> list[Operator]:
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError('文件必须是 JSON 数组：[{"operator_id": ..., "display_name": ..., "role": ...}]')
    return [Operator.model_validate(item) for item in raw]


async def _run(path: Path, *, dsn: str | None, dry_run: bool) -> dict[str, Any]:
    operators = _load(path)
    if dry_run:
        return {"total": len(operators), "landed": 0, "dry_run": True}
    store = PostgresStore(dsn=dsn or None)
    try:
        await store.connect()
        landed = await OperatorService(store).seed(operators)
    finally:
        await store.close()
    return {"total": len(operators), "landed": landed, "dry_run": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入值班席位（幂等 upsert）")
    parser.add_argument("--file", required=True, type=Path, help="JSON 数组文件")
    parser.add_argument("--dsn", default=None, help="覆盖 AEGIS_PG_DSN")
    parser.add_argument("--dry-run", action="store_true", help="只校验，不写库")
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_run(args.file, dsn=args.dsn, dry_run=args.dry_run))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
