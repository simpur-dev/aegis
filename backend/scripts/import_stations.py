"""站点台账导入：给 `upsert_station` 一个生产入口（此前它只有测试调用方）。

为什么需要这条命令：`GET /api/v1/stations` 的清单里，站点名称与坐标只能来自维表，
而维表从来没有任何生产写入方——于是"一张图"上每个站都是 `name_zh=''`、坐标 null，
前端只能把它们全列进"未定位"。这不是渲染问题，是入口缺失。

口径（都是能在验收时被追问的）：
- **不猜坐标**：经纬要么成对给出，要么留空；留空的站照样导入，只是不上地图。
  按区划中心或站号推一个经纬度，演示里看不出差别，被当成实测证据就是事故。
- **先全量校验再写**：任何一行不合法就整份不导入。部分导入会让"这个区有几个站"
  在不同批次之间漂移，比直接失败难查得多。
- **列名不认识就失败**：`logitude` 这种拼错的列会被静默丢掉，坐标于是全空。
- **内存视图默认拒绝**：内存维表活到进程结束为止，导入它等于没导入；
  确要做演示可以显式 `--allow-memory`，结果里会写明 `durable=false`。

用法：
    uv run python -m scripts.import_stations --source stations.csv            # 校验并导入
    uv run python -m scripts.import_stations --source stations.jsonl --dry-run # 只校验
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REGION_PATTERN = re.compile(r"^[0-9A-Z]{6,24}$")
MAX_ID_CHARS = 64
MAX_NAME_CHARS = 120
MAX_FOCUS_ITEMS = 12
MAX_FOCUS_CHARS = 64
KNOWN_COLUMNS = ("station_id", "region_code", "lon", "lat", "name_zh", "hazard_focus", "elevation_m")


@dataclass(frozen=True)
class StationRecord:
    station_id: str
    region_code: str
    lon: float | None
    lat: float | None
    name_zh: str = ""
    hazard_focus: tuple[str, ...] = ()
    elevation_m: float | None = None

    @property
    def positioned(self) -> bool:
        return self.lon is not None and self.lat is not None


class SourceProblem(Exception):
    """一条输入行的问题；行号让人能在原文件里定位，不必先复现解析。"""

    def __init__(self, line: int, reason: str) -> None:
        super().__init__(f"第 {line} 行: {reason}")
        self.line = line
        self.reason = reason


def _as_float(value: Any, *, field: str, line: int) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SourceProblem(line, f"{field} 不是数字：{value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise SourceProblem(line, f"{field} 必须是有限数值：{value!r}")
    return number


def _clean_text(value: Any, *, field: str, line: int, max_chars: int) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > max_chars:
        raise SourceProblem(line, f"{field} 超过 {max_chars} 字")
    if any(ord(char) < 32 for char in text):
        raise SourceProblem(line, f"{field} 含控制字符")
    return text


def _focus(value: Any, *, line: int) -> tuple[str, ...]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return ()
    items = value if isinstance(value, list) else tuple(part for part in re.split(r"[|;,]", str(value)) if part.strip())
    if len(items) > MAX_FOCUS_ITEMS:
        raise SourceProblem(line, f"hazard_focus 最多 {MAX_FOCUS_ITEMS} 项")
    cleaned: list[str] = []
    for item in items:
        text = _clean_text(item, field="hazard_focus", line=line, max_chars=MAX_FOCUS_CHARS)
        if text:
            cleaned.append(text)
    return tuple(cleaned)


def parse_record(raw: dict[str, Any], *, line: int) -> StationRecord:
    unknown = sorted(key for key in raw if key not in KNOWN_COLUMNS)
    if unknown:
        raise SourceProblem(line, f"未知列 {unknown}：认识的列才允许，拼错的列会被静默丢掉")

    station_id = _clean_text(raw.get("station_id"), field="station_id", line=line, max_chars=MAX_ID_CHARS)
    if not station_id:
        raise SourceProblem(line, "station_id 不能为空")
    region_code = _clean_text(raw.get("region_code"), field="region_code", line=line, max_chars=24).upper()
    if not REGION_PATTERN.match(region_code):
        raise SourceProblem(line, f"region_code 不满足 6-24 位大写区划码：{region_code!r}")

    lon = _as_float(raw.get("lon"), field="lon", line=line)
    lat = _as_float(raw.get("lat"), field="lat", line=line)
    if (lon is None) != (lat is None):
        raise SourceProblem(line, "坐标必须成对给出（只给一半通常是漏填，不是没有）")
    if lon is not None and not -180.0 <= lon <= 180.0:
        raise SourceProblem(line, f"lon 超出 [-180, 180]：{lon}")
    if lat is not None and not -90.0 <= lat <= 90.0:
        raise SourceProblem(line, f"lat 超出 [-90, 90]：{lat}")

    elevation = _as_float(raw.get("elevation_m"), field="elevation_m", line=line)
    return StationRecord(
        station_id=station_id,
        region_code=region_code,
        lon=lon,
        lat=lat,
        name_zh=_clean_text(raw.get("name_zh"), field="name_zh", line=line, max_chars=MAX_NAME_CHARS),
        hazard_focus=_focus(raw.get("hazard_focus"), line=line),
        elevation_m=elevation,
    )


def read_rows(source: Path) -> list[tuple[int, dict[str, Any]]]:
    """返回 (行号, 原始记录)。行号按输入文件算，方便现场照着改。"""
    text = source.read_text(encoding="utf-8-sig")
    if source.suffix == ".json":
        raise ValueError("只吃 .csv 或 .jsonl/.ndjson：整个 JSON 数组没有行号，出错时定位不到具体站")
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
        raise ValueError("CSV 没有表头：无法判断哪一列是坐标")
    header = [(index + 2, {key: value for key, value in row.items() if key is not None}) for index, row in enumerate(reader)]
    return header


def load_records(source: Path) -> tuple[list[StationRecord], list[str]]:
    """整份解析并返回 (记录, 问题列表)；有问题时记录不可信，调用方必须放弃导入。"""
    problems: list[str] = []
    records: list[StationRecord] = []
    seen: dict[str, int] = {}
    try:
        rows = read_rows(source)
    except (OSError, ValueError) as exc:
        return [], [str(exc)]
    if not rows:
        return [], ["输入文件为空：一份空台账导入成功，只会让「这个区一个站都没有」看起来正常"]
    for line, raw in rows:
        try:
            record = parse_record(raw, line=line)
        except SourceProblem as exc:
            problems.append(str(exc))
            continue
        if record.station_id in seen:
            problems.append(f"第 {line} 行: station_id {record.station_id} 与第 {seen[record.station_id]} 行重复")
            continue
        seen[record.station_id] = line
        records.append(record)
    return records, problems


async def import_records(store: Any, records: list[StationRecord]) -> int:
    for record in records:
        await store.upsert_station(
            record.station_id,
            record.region_code,
            record.lon,
            record.lat,
            name_zh=record.name_zh,
            hazard_focus=record.hazard_focus,
            elevation_m=record.elevation_m,
        )
    return len(records)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入站点台账（不猜坐标、全量校验后才写）")
    parser.add_argument("--source", type=Path, required=True, help="站点台账 CSV 或 JSONL")
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
                    "positioned": sum(1 for record in records if record.positioned),
                    "unpositioned": sum(1 for record in records if not record.positioned),
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
                {"status": "refused", "reason": "store_backend 不是 postgres：内存维表活到进程结束为止"}, ensure_ascii=False, indent=2
            ),
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
                "positioned": sum(1 for record in records if record.positioned),
                "unpositioned": sum(1 for record in records if not record.positioned),
                "durable": state.driver == "postgres",
                "store_driver": state.driver,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if state.driver != "postgres":
        print("提醒：这份台账只活在本进程里（store_driver=memory）。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
