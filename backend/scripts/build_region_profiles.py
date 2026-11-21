"""区域历史灾害画像生成：外部公开数据集 → `site_profiles` 属性 → 研判证据包。

**这一步解决的是"LLM 研判没有历史上下文"**：研判 LLM 看到的证据是窗口读数 +
站点画像，但不知道"这个县近 10 年灾害构成是什么样"——而那正是有经验的值班员
判断时会用的先验。外部公开数据目录里的公路地灾普查（13,768 条，2012–2022，
逐条带区划/日期/灾种）第一次让这个先验**有据可查**。

**聚合口径（全部可回溯到普查条目）**：
- 按区划 × 灾种统计近 10 年（2013-01-01 起，含发生与隐患条目）事件数；
- 灾种构成占比 → `history_dominant_hazard`（class）；
- 高发月份 → `history_peak_month`（class，1–12）；
- 年度峰值 → `history_peak_year`（class）；
- 属性 key 全部走 `history_` 前缀 + 小写下划线（SiteAttribute key 校验约束），
  source 统一登记数据集 id（CC BY-NC 留痕：商用发行前随目录移除）。

**进证据包的路径（零契约改动）**：`import_site_profiles` 导入 PG →
`assess_input._site_observations` 把每条 attribute 变成 FactorObservation
（数值→max_value，字符串→class_value，quality="site"）→ 研判 LLM 在证据里
直接读到历史构成。历史 key 不在任何灾种因子配置里——**不会进因子分**，
覆盖率门控不受影响（只增强 LLM 的上下文，不改确定性算法）。

**如实声明**：普查是公路沿线的调查统计（覆盖公路两侧，不是全县面覆盖），
统计值是"公路台账里的记录构成"，不是区域灾害总量——证据包里以 source 标明，
研判提示词明确"先验不是证据，不得据此抬等级"。

用法：
    uv run python scripts/build_region_profiles.py --out backend/data/site_profiles_history.jsonl
    uv run python -m scripts.import_site_profiles --source backend/data/site_profiles_history.jsonl
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CENSUS_PATH = REPO_ROOT / "data" / "external" / "sciencedb-tibet-road-geohazard-2022" / "normalized" / "tibet_road_geohazard_2022.v1.jsonl"
GLOF_PATH = REPO_ROOT / "data" / "external" / "glof-db" / "normalized" / "glof_events.v1.jsonl"
DEFAULT_OUT = REPO_ROOT / "backend" / "data" / "site_profiles_history.jsonl"
SOURCE_CENSUS = "sciencedb-road-census"
SOURCE_GLOF = "glof-db"
SOURCE_GLC = "nasa-glc"
GLC_PATH = REPO_ROOT / "data" / "external" / "nasa-glc" / "normalized" / "china_landslide_events.v1.jsonl"
#: 近 10 年窗口（普查记录 occurrence_window_years=10 的口径对齐）
WINDOW_START = "2013-01-01"
PROVENANCE_NOTE = "公路沿线台账统计（非全县面覆盖）；先验供研判参考，不得据此抬等级"


def _hazard_cn(hazard: str) -> str:
    return {"rockfall": "崩塌", "debris_flow": "泥石流", "landslide": "滑坡"}.get(hazard, hazard)


def load_census_stats(path: Path) -> dict[str, dict[str, Any]]:
    """普查 → 按区划聚合 {events_10y, by_hazard, by_month, by_year, dominant}。"""
    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "counts": Counter(),
            "months": Counter(),
            "years": Counter(),
            "records": 0,
        }
    )
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        region = (row["geo"].get("admin_division") or "").strip()
        day = (row["event"].get("date") or "").strip()
        if not region or not day or day < WINDOW_START:
            continue
        bucket = stats[region]
        bucket["records"] += 1
        hazard = row["hazard"]["type"]
        if hazard != "unknown":
            bucket["counts"][hazard] += 1
        bucket["months"][int(day[5:7])] += 1
        bucket["years"][day[:4]] += 1

    out: dict[str, dict[str, Any]] = {}
    for region, bucket in stats.items():
        hazards = dict(bucket["counts"])
        dominant = max(hazards, key=hazards.get) if hazards else None
        out[region] = {
            "total": bucket["records"],
            "hazards": hazards,
            "dominant": dominant,
            "peak_month": bucket["months"].most_common(1)[0][0] if bucket["months"] else None,
            "peak_year": bucket["years"].most_common(1)[0][0] if bucket["years"] else None,
        }
    return out


def load_glof_china_total(path: Path) -> int | None:
    """GLOF DB 里 High Mountain Asia 表的中国境内事件总数（全区口径，供参考）。"""
    if not path.exists():
        return None
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("geo", {}).get("country") == "China":
            count += 1
    return count


def load_glc_regions(path: Path) -> dict[str, int]:
    """NASA GLC 中国子集按区划聚合（行政区划取 geo.admin_division，多数为空则给全区口径）。"""
    if not path.exists():
        return {}
    counts: Counter = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        region = (row.get("geo", {}).get("admin_division") or "").strip()
        if region:
            counts[region] += 1
    return dict(counts)


def load_existing(path: Path | None) -> dict[str, dict[str, Any]]:
    """既有站点画像（按 region_code）：合并目标。

    **为什么必须合并而不是覆盖**：`import_site_profiles` 是 upsert（整条替换）——
    若历史画像独占一个 region_code，导入就会把既有普查属性（坡度/汇水面积/沟道比降）
    冲掉，因子引擎当场判 P 组覆盖率 0%、预警素材腿拿不到 `assessment_status=scored`
    的信封（2026-11-21 真机联调实测到的连锁故障）。历史统计是**追加维度**，
    与现场普查属性共存于同一画像。
    """
    if path is None or not path.is_file():
        return {}
    existing: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        region = str(row.get("region_code") or "").strip()
        if region:
            existing[region] = row
    return existing


def build_profiles(
    census_path: Path,
    glof_path: Path,
    glc_path: Path,
    surveyed_at: str,
    *,
    existing_path: Path | None = None,
) -> list[dict[str, Any]]:
    census = load_census_stats(census_path)
    glc_regions = load_glc_regions(glc_path)
    existing = load_existing(existing_path)

    profiles: list[dict[str, Any]] = []
    for region, stat in sorted(census.items()):
        attributes: list[dict[str, Any]] = []
        for hazard, count in sorted(stat["hazards"].items(), key=lambda kv: -kv[1]):
            attributes.append(
                {
                    "key": f"history_{hazard}_events_10y",
                    "value": float(count),
                    "unit": "起",
                    "source": SOURCE_CENSUS,
                    "surveyed_at": surveyed_at,
                }
            )
        attributes.append(
            {
                "key": "history_total_events_10y",
                "value": float(stat["total"]),
                "unit": "起",
                "source": SOURCE_CENSUS,
                "surveyed_at": surveyed_at,
            }
        )
        if stat["dominant"]:
            attributes.append(
                {
                    "key": "history_dominant_hazard",
                    "value": _hazard_cn(stat["dominant"]),
                    "source": SOURCE_CENSUS,
                    "surveyed_at": surveyed_at,
                }
            )
        if stat["peak_month"]:
            attributes.append(
                {"key": "history_peak_month", "value": str(stat["peak_month"]) + "月", "source": SOURCE_CENSUS, "surveyed_at": surveyed_at}
            )
        if stat["peak_year"]:
            attributes.append(
                {"key": "history_peak_year", "value": stat["peak_year"] + "年", "source": SOURCE_CENSUS, "surveyed_at": surveyed_at}
            )
        hazards_in_region = sorted(stat["hazards"])
        hazard_types = [h for h in ("debris_flow", "landslide", "rockfall") if h in hazards_in_region]
        if glc_regions.get(region):
            attributes.append(
                {
                    "key": "history_glc_landslides",
                    "value": float(glc_regions[region]),
                    "unit": "起",
                    "source": SOURCE_GLC,
                    "surveyed_at": surveyed_at,
                }
            )
        base = existing.get(region)
        if base is not None:
            base_keys = {str(attr.get("key")) for attr in base.get("attributes") or []}
            merged_attributes = list(base.get("attributes") or []) + [a for a in attributes if a["key"] not in base_keys]
            name = str(base.get("name") or f"{region} 站点画像")
            merged_hazards = sorted({str(h) for h in (base.get("hazard_types") or [])} | set(hazard_types))
            profiles.append(
                {
                    "region_code": region,
                    "name": name,
                    "hazard_types": merged_hazards,
                    "attributes": merged_attributes,
                    "provenance": base.get("provenance")
                    or {
                        "data_kind": "derived",
                        "sources": [SOURCE_CENSUS],
                        "disclaimer": PROVENANCE_NOTE,
                    },
                }
            )
            continue
        profiles.append(
            {
                "region_code": region,
                "name": f"{region} 历史灾害画像（数据集聚合）",
                "hazard_types": hazard_types,
                "attributes": attributes,
                # provenance 形状 = DataProvenance（data_kind/sources/method/disclaimer）
                "provenance": {
                    "data_kind": "derived",
                    "sources": [SOURCE_CENSUS, SOURCE_GLOF, SOURCE_GLC],
                    "method": "unknown",
                    "disclaimer": PROVENANCE_NOTE,
                },
            }
        )
    return profiles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="区域历史灾害画像：外部数据集 → site_profiles 属性")
    parser.add_argument("--census", type=Path, default=CENSUS_PATH)
    parser.add_argument("--glof", type=Path, default=GLOF_PATH)
    parser.add_argument("--glc", type=Path, default=GLC_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--merge-existing",
        type=Path,
        default=REPO_ROOT / "backend" / "data" / "site_profiles.demo.jsonl",
        help="既有站点画像（按 region_code 合并历史属性，不覆盖普查属性）",
    )
    args = parser.parse_args(argv)

    from datetime import UTC, datetime

    surveyed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    profiles = build_profiles(args.census, args.glof, args.glc, surveyed_at, existing_path=args.merge_existing)
    glof_total = load_glof_china_total(args.glof)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as handle:
        for profile in profiles:
            handle.write(json.dumps(profile, ensure_ascii=False) + "\n")
    print(f"画像：{args.out}（{len(profiles)} 个区划；GLOF 中国境内 {glof_total} 事件为全区参考口径，不逐县下放）")
    sample = next((p for p in profiles if p["region_code"] == "540121"), profiles[0] if profiles else None)
    if sample:
        attrs = {a["key"]: a["value"] for a in sample["attributes"]}
        print("示例（540121）:", json.dumps(attrs, ensure_ascii=False)[:300])
    # 覆盖度如实报：只有同时具备现场普查属性的区划才能过因子引擎的 P 组门控；
    # 其余区划的历史画像只喂 LLM 先验（不编造普查值填门控）。
    with_survey = [p for p in profiles if any(not a["key"].startswith("history_") for a in p["attributes"])]
    print(f"覆盖度：{len(with_survey)}/{len(profiles)} 个区划同时有现场普查属性（可过因子引擎 P 组门控），其余仅有历史先验")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
