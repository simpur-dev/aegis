"""历史回放数据集构建：公路普查真值 × CHM_PRE 真实降雨 → 逐日逐区域回放样本。

**这一步解决的是"准确性没法测"**：优化决策的前提是先能度量。外部公开数据目录里的
公路地灾普查（CC BY-NC，13,768 条，2012–2022）提供**真值**（某县某日是否发生了
某类灾害），CHM_PRE 派生表（运行态，2020–2022）提供**真实降雨**——两者按
（区域, 日期）对齐，就是一份带真值的决策回放集：

- **正样本**：已发生（occurred=true）且有日期的普查记录 → 当日该区域该灾种 truth_warning=true；
- **负样本**：同一区域当月其余日期（该月无任何该类记录）→ truth_warning=false——
  按**月**取负样本而不是全年随机：汛期/旱季的降雨基线不同，随机抽会把旱季的零降雨
  当"容易判的负样本"，虚高指标；
- 一天可能多灾种（泥石流+滑坡同日）：按灾种各自独立算正负（评测按灾种分列）。

**口径（都能被追问的）**：
- 只收普查里三县（540121/540221/540321）——这三县有 CHM_PRE 派生表的真实降雨；
  其余区划没有降雨数据，硬凑就是编造；
- 降雨取**县域代理点当日值**（station_daily.csv），缺测日如实标 `rain_mm=null`，
  回放评测跳过该日；
- 灾种映射：普查类型 → 平台 HazardType（rockfall/debris_flow/landslide 直取；
  unknown 不参与分灾种评测，但参与"任意灾害发生"的总体口径）。

**设计边界（如实记）**：负样本只在该区该月**至少有一次该灾种事件**时展开生成
（迭代事件日时把同月其余日期作为该灾种的负样本）。因此"整月无该灾种"的月份不产负样本，
precision 是"有事件月份内"的条件估计——它防的是"抓没抓住事件"，不是"全年误报率"。

**用途**：`scripts/replay_rule_accuracy.py` 在这份样本上评测现行规则库与数据驱动
候选规则的准确率（precision/recall/F1）。普查数据是 CC BY-NC——样本含普查字段，
落运行态目录（backend/data/replay/），**不进 git**（与 NC 隔离纪律一致）。

用法：
    uv run python scripts/build_replay_dataset.py
    uv run python scripts/build_replay_dataset.py --out backend/data/replay
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CENSUS_PATH = REPO_ROOT / "data" / "external" / "sciencedb-tibet-road-geohazard-2022" / "normalized" / "tibet_road_geohazard_2022.v1.jsonl"
STATION_RAIN = REPO_ROOT / "backend" / "data" / "precip" / "station_daily.csv"
DEFAULT_OUT = REPO_ROOT / "backend" / "data" / "replay"
#: 三县 = CHM_PRE 派生表覆盖的区域（站点表同源）。其余区划没有真实降雨，不硬凑。
STATION_REGIONS = {"540121", "540221", "540321"}
#: 回放窗口：与 CHM_PRE 派生表（2020–2022）对齐
REPLAY_YEARS = ("2020", "2021", "2022")
#: 普查灾种 → 平台 HazardType（unknown 不进分灾种评测）
TYPE_MAP = {"rockfall": "rockfall", "debris_flow": "debris_flow", "landslide": "landslide"}


def load_census(path: Path) -> dict[tuple[str, str], list[str]]:
    """普查 → {(region, 'YYYY-MM-DD'): [灾种…]}，仅限已发生 + 有日期 + 三县 + 回放窗口。"""
    events: dict[tuple[str, str], set[str]] = defaultdict(set)
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        total += 1
        row = json.loads(line)
        attributes = row["hazard"]["attributes"]
        if attributes.get("occurred") is not True:
            continue
        region = (row["geo"].get("admin_division") or "").strip()
        day = (row["event"].get("date") or "").strip()
        if region not in STATION_REGIONS or not day.startswith(REPLAY_YEARS):
            continue
        hazard = TYPE_MAP.get(row["hazard"]["type"])
        if hazard is None:
            continue
        events[(region, day)].add(hazard)
    counted = sum(len(v) for v in events.values())
    print(f"普查 {total} 条 → 三县回放窗口内已发生事件 {counted} 条（{len(events)} 个区域-日）")
    return {(region, day): sorted(types) for (region, day), types in events.items()}


def load_rain(path: Path) -> dict[tuple[str, str], float | None]:
    """派生表 → {(region, 'YYYY-MM-DD'): 当日降雨 mm（缺测 None）。"""
    rain: dict[tuple[str, str], float | None] = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (row["region_code"], row["date"])
            raw = (row.get("precip_mm") or "").strip()
            rain[key] = None if not raw else float(raw)
    return rain


def month_key(day: str) -> str:
    return day[:7]


def build_cases(census_path: Path, rain_path: Path) -> dict[str, Any]:
    events = load_census(census_path)
    rain = load_rain(rain_path)
    # 负样本 = 同区域、同月、且当日无该灾种事件的日期（按月对齐降雨基线）
    month_days: dict[tuple[str, str], list[tuple[str, float | None]]] = defaultdict(list)
    for (region, day), value in sorted(rain.items()):
        if day.startswith(REPLAY_YEARS):
            month_days[(region, month_key(day))].append((day, value))

    cases: list[dict[str, Any]] = []
    skipped_no_rain = 0
    seen: set[tuple[str, str, str]] = set()
    for (region, day), hazards in sorted(events.items()):
        value = rain.get((region, day))
        if value is None:
            skipped_no_rain += 1
            continue
        month = month_key(day)
        for hazard in hazards:
            case_id = f"replay:{region}:{day}:{hazard}"
            if (region, day, hazard) in seen:
                continue
            seen.add((region, day, hazard))
            cases.append(
                {
                    "case_id": case_id,
                    "region_code": region,
                    "date": day,
                    "hazard_type": hazard,
                    "rain_mm": value,
                    "truth_warning": True,
                    "truth_hazards": hazards,
                    "source": "sciencedb-tibet-road-geohazard-2022",
                }
            )
            # 同月其余日期 = 该灾种的负样本（当月无该类记录）
            for other_day, other_rain in month_days[(region, month)]:
                if other_day == day:
                    continue
                # 键必须是 (region, day) 元组——用字符串键查元组字典永远落空，
                # 事件日会被当成负样本追加（2026-11-21 复检抓到的真 bug：123/170 事件日被误标）。
                other_events = events.get((region, other_day), [])
                if hazard in other_events:
                    continue  # 同月同灾种另一天的事件日：不当负样本
                if other_rain is None:
                    continue
                cases.append(
                    {
                        "case_id": f"replay:{region}:{other_day}:{hazard}",
                        "region_code": region,
                        "date": other_day,
                        "hazard_type": hazard,
                        "rain_mm": other_rain,
                        "truth_warning": False,
                        "truth_hazards": [],
                        "source": "derived-negative",
                    }
                )

    # 去重（同一 region|day|hazard 可能被多个月份窗口生成）。**正样本优先**：
    # 按插入顺序"先到先留"会让先生成的负样本压掉同一天的真事件（真值被标签错误覆盖）。
    unique: dict[str, dict[str, Any]] = {}
    for case in cases:
        existing = unique.get(case["case_id"])
        if existing is None or (case["truth_warning"] and not existing["truth_warning"]):
            unique[case["case_id"]] = case
    ordered = sorted(unique.values(), key=lambda c: (c["region_code"], c["date"], c["hazard_type"]))
    return {
        "schema_version": "1.0.0",
        "generated_note": "普查真值 × CHM_PRE 真实降雨；负样本按同月对齐；NC 数据落运行态不入 git",
        "counts": {
            "cases": len(ordered),
            "positive": sum(1 for c in ordered if c["truth_warning"]),
            "negative": sum(1 for c in ordered if not c["truth_warning"]),
            "skipped_event_days_no_rain": skipped_no_rain,
        },
        "by_hazard": {
            hazard: {
                "positive": sum(1 for c in ordered if c["hazard_type"] == hazard and c["truth_warning"]),
                "negative": sum(1 for c in ordered if c["hazard_type"] == hazard and not c["truth_warning"]),
            }
            for hazard in sorted({c["hazard_type"] for c in ordered})
        },
        "cases": ordered,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="普查真值 × 真实降雨 → 决策回放数据集")
    parser.add_argument("--census", type=Path, default=CENSUS_PATH, help="普查 normalized JSONL")
    parser.add_argument("--rain", type=Path, default=STATION_RAIN, help="CHM_PRE 派生站点逐日表")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录")
    args = parser.parse_args(argv)

    if not args.rain.exists():
        raise SystemExit(f"派生表不存在：{args.rain}——先跑 fetch/derive（2020–2022）")
    report = build_cases(args.census, args.rain)
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "replay_cases.jsonl"
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for case in report["cases"]:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    meta_path = args.out / "replay_meta.json"
    meta = {k: v for k, v in report.items() if k != "cases"}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"样本：{out_path}（{report['counts']['cases']} 条，正 {report['counts']['positive']} / 负 {report['counts']['negative']}）")
    print(f"元信息：{meta_path}")
    print("按灾种：", json.dumps(report["by_hazard"], ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
