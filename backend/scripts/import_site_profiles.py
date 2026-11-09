"""区域级站点画像导入：给 `upsert_site_profile` 一个生产入口。

画像只放静态/慢变勘测属性（坡度、流域面积、冰湖库容…）。雨量、位移速率、湖水位这类
动态触发量属于遥测，写进画像会让同一个因子出现两个口径——所以本脚本只认 `SiteProfile`
的形状，不提供"往画像里追加任意列"的旁路。

口径与 `import_stations.py` 一致：
- **先全量校验再写**：任何一行不合法就整份不导入。
- **列名不认识就失败**：拼错的列会被静默丢掉，导进去就是一份缺属性的画像。
- **重复 region 失败**：一个区只有一条画像，后写覆盖前写会让"导入了什么"无法复盘。
- **内存视图默认拒绝**：内存画像活到进程结束为止；演示可显式 `--allow-memory`，
  结果里会写明 `durable=false`。

用法：
    uv run python -m scripts.import_site_profiles --source data/site_profiles.demo.jsonl
    uv run python -m scripts.import_site_profiles --source ... --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aegis.domain.site import SiteProfile

KNOWN_COLUMNS = ("region_code", "name", "hazard_types", "attributes", "provenance")


def read_rows(source: Path) -> list[tuple[int, dict[str, Any]]]:
    """返回 (行号, 原始记录)。行号按输入文件算，方便现场照着改。"""
    text = source.read_text(encoding="utf-8-sig")
    if source.suffix == ".json":
        raise ValueError("只吃 .csv 或 .jsonl/.ndjson：整个 JSON 数组没有行号，出错时定位不到具体画像")
    if source.suffix in {".jsonl", ".ndjson"}:
        rows: list[tuple[int, dict[str, Any]]] = []
        for index, chunk in enumerate(text.splitlines(), start=1):
            if not chunk.strip():
                continue
            try:
                parsed = json.loads(chunk)
            except json.JSONDecodeError as exc:
                raise ValueError(f"第 {index} 行不是合法 JSON：{exc.msg}") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"第 {index} 行不是对象")
            rows.append((index, parsed))
        return rows
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise ValueError("CSV 没有表头：无法判断哪一列是区划码")
    return [(index + 2, {key: value for key, value in row.items() if key is not None}) for index, row in enumerate(reader)]


def _decode_field(raw: dict[str, Any], key: str, *, line: int) -> Any:
    """CSV 里的嵌套字段是 JSON 文本；JSONL 里已经是对象/数组，原样透传。"""
    value = raw.get(key)
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"第 {line} 行: {key} 不是合法 JSON：{exc.msg}") from exc


def parse_record(raw: dict[str, Any], *, line: int) -> SiteProfile:
    unknown = sorted(key for key in raw if key not in KNOWN_COLUMNS)
    if unknown:
        raise ValueError(f"第 {line} 行: 未知列 {unknown}：认识的列才允许，拼错的列会被静默丢掉")
    payload: dict[str, Any] = {
        "region_code": raw.get("region_code"),
        "name": raw.get("name") or "",
        "hazard_types": _decode_field(raw, "hazard_types", line=line) or [],
        "attributes": _decode_field(raw, "attributes", line=line) or [],
    }
    provenance = _decode_field(raw, "provenance", line=line)
    if provenance is not None:
        payload["provenance"] = provenance
    try:
        return SiteProfile.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ()))
        raise ValueError(f"第 {line} 行: {where} {first.get('msg', '不合契约')}") from exc


def load_records(source: Path) -> tuple[list[SiteProfile], list[str]]:
    """整份解析并返回 (画像, 问题列表)；有问题时调用方必须放弃导入。"""
    problems: list[str] = []
    records: list[SiteProfile] = []
    seen: dict[str, int] = {}
    try:
        rows = read_rows(source)
    except (OSError, ValueError) as exc:
        return [], [str(exc)]
    if not rows:
        return [], ["输入文件为空：一份空画像导入成功，只会让「这个区没有勘测属性」看起来正常"]
    for line, raw in rows:
        try:
            profile = parse_record(raw, line=line)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        if profile.region_code in seen:
            problems.append(f"第 {line} 行: region_code {profile.region_code} 与第 {seen[profile.region_code]} 行重复")
            continue
        seen[profile.region_code] = line
        records.append(profile)
    return records, problems


async def import_records(store: Any, records: list[SiteProfile]) -> int:
    for profile in records:
        await store.upsert_site_profile(profile)
    return len(records)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入区域级站点画像（全量校验后才写）")
    parser.add_argument("--source", type=Path, required=True, help="站点画像 CSV 或 JSONL")
    parser.add_argument("--dry-run", action="store_true", help="只校验不写入")
    parser.add_argument("--allow-memory", action="store_true", help="允许写进内存视图（进程结束即消失）")
    args = parser.parse_args(argv)

    records, problems = load_records(args.source)
    if problems:
        print(json.dumps({"status": "rejected", "problems": problems[:20], "problem_count": len(problems)}, ensure_ascii=False, indent=2))
        print("整份未导入：修完这些行再来一次。", file=sys.stderr)
        return 1

    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "validated",
                    "rows": len(records),
                    "attributes": sum(len(profile.attributes) for profile in records),
                    "demo_rows": sum(1 for profile in records if profile.provenance.data_kind == "demo"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    from aegis.config import get_settings
    from aegis.integrations import build_store, start_store, stop_store

    settings = get_settings()
    bundle = build_store(settings)
    if bundle.durability is None and not args.allow_memory:
        print(
            json.dumps(
                {"status": "refused", "reason": "store_backend 不是 postgres：内存画像活到进程结束为止"},
                ensure_ascii=False,
                indent=2,
            )
        )
        print("要确实往内存视图里灌（只为演示）就加 --allow-memory。", file=sys.stderr)
        return 2

    state = await start_store(bundle, settings)
    try:
        imported = await import_records(bundle.store, records)
    finally:
        await stop_store(bundle)
    print(
        json.dumps(
            {
                "status": "imported",
                "rows": imported,
                "attributes": sum(len(profile.attributes) for profile in records),
                "durable": state.driver == "postgres",
                "store_driver": state.driver,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if state.driver != "postgres":
        print("提醒：这份画像只活在本进程里（store_driver=memory）。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
