"""规则库版本化的门禁（批次 C2 / ADR-0006）。

最要紧的一条是"两处阈值必须一致"：`default_rulebook()` 是运行时的唯一判据，
`005_trigger_rules.sql` 是库里的种子。这两份一旦漂移，"现场用的是哪一版"就没有唯一答案，
而回滚也会滚到另一套判据上去。所以这里直接解析 SQL 文本逐字段比对，而不是靠人记住。
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from aegis.domain.enums import HazardType, RiskLevel
from aegis.services.risk_engine import RiskEngine
from aegis.services.trigger_rules import (
    SEED_CALIBRATION_BASIS,
    Condition,
    RuleEngine,
    default_rulebook,
    rule_from_row,
    rulebook_from_rows,
)

SQL = Path(__file__).resolve().parents[2] / "src" / "aegis" / "persistence" / "sql" / "005_trigger_rules.sql"

#: 种子行的形状（列顺序与 INSERT 列表一致；conditions 刻意写成单行 JSON 才好稳定解析）
_SEED_ROW = re.compile(
    r"\(\s*'(R-[A-Z0-9-]+)',\s*(\d+),\s*'(\w+)',\s*'(\w+)',\s*'([^']+)',\s*'(\w+)',\s*(\d+),\s*([\d.]+),"
    r"\s*'(\[.*?\])'::jsonb,\s*'([^']+)',\s*'([^']+)',\s*now\(\)\s*\)",
)


def _seed_rows() -> list[dict[str, Any]]:
    text = SQL.read_text(encoding="utf-8")
    rows: list[dict[str, Any]] = []
    for match in _SEED_ROW.finditer(text):
        rule_id, version, status, hazard, description, mode, level, weight, conditions, basis, reviewer = match.groups()
        rows.append(
            {
                "rule_id": rule_id,
                "version": int(version),
                "status": status,
                "hazard_type": hazard,
                "description": description,
                "mode": mode,
                "triggered_level": int(level),
                "weight": float(weight),
                "conditions": json.loads(conditions),
                "calibration_basis": basis,
                "reviewer": reviewer,
            }
        )
    return rows


def test_种子解析器本身不空转() -> None:
    """先确认量到东西：正则一旦与 SQL 排版脱节，比对会退化成"两边都空所以相等"。"""
    rows = _seed_rows()
    assert len(rows) == 9, f"从 005_trigger_rules.sql 解析到 {len(rows)} 行种子"
    assert {row["hazard_type"] for row in rows} == {"debris_flow", "landslide", "rockfall", "avalanche", "lake_outburst"}


def test_代码种子与库种子逐字段一致() -> None:
    by_id = {row["rule_id"]: row for row in _seed_rows()}
    rules = {rule.rule_id: rule for rule in default_rulebook()}
    assert set(by_id) == set(rules)
    keys = (
        "rule_id",
        "version",
        "status",
        "hazard_type",
        "description",
        "mode",
        "triggered_level",
        "weight",
        "conditions",
        "calibration_basis",
        "reviewer",
    )
    drift = [rule_id for rule_id, rule in rules.items() if rule.as_row() != {key: by_id[rule_id][key] for key in keys}]
    assert not drift, f"两份阈值漂移了: {drift}"


def test_内置种子如实标注未标定() -> None:
    """未标定的值必须带着"未标定"出门，否则报表会把代码值说成专家结论。"""
    assert all(SEED_CALIBRATION_BASIS in rule.calibration_basis for rule in default_rulebook())
    assert RuleEngine().describe()["uncalibrated"] == 9


def test_触发条件种类与灾种覆盖达到指标口径() -> None:
    """指标 1 说的是"识别 ≥5 类触发条件"：这里量的是真实种类数，不是规则条数。"""
    engine = RuleEngine()
    metrics = {condition.metric for rule in engine.rules for condition in rule.conditions}
    hazards = {rule.hazard_type for rule in engine.rules}
    assert len(metrics) >= 5, sorted(metrics)
    assert len(hazards) >= 5, sorted(item.value for item in hazards)
    assert engine.describe()["hazards"] == sorted(item.value for item in hazards)


# ---------- 行 → 规则 ----------


def test_库行装配保留版本与出处() -> None:
    row = _seed_rows()[1]
    rule = rule_from_row(row)
    assert rule.rule_id == "R-DEBRIS-RAIN-2"
    assert rule.version == 1
    assert rule.status == "active"
    assert rule.triggered_level is RiskLevel.RED
    assert rule.conditions[0] == Condition("rain_cumulative_24h", ">=", 80.0, "max", 86_400)


def test_conditions_可以是_JSON_文本() -> None:
    row = dict(_seed_rows()[0])
    row["conditions"] = json.dumps(row["conditions"])
    assert rule_from_row(row).conditions[0].threshold == 30.0


@pytest.mark.parametrize(
    ("patch", "why"),
    [
        ({"_where": "condition", "op": "~="}, "不支持的比较算子"),
        ({"_where": "row", "_key": "hazard_type", "hazard_type": "tsunami"}, "tsunami"),
        ({"_where": "row", "_key": "triggered_level", "triggered_level": 9}, "1 AND 5|越界|range"),
        ({"_where": "row", "_key": "conditions", "conditions": []}, "至少含一个条件"),
        ({"_where": "condition", "agg": "median"}, "不支持的聚合方式"),
        ({"_where": "condition", "window_seconds": 0}, "必须为正"),
    ],
)
def test_坏行进拒绝清单而不是静默丢掉(patch: dict[str, Any], why: str) -> None:
    """丢一条规则就是丢一类触发条件，而报表上完全看不出来——所以坏行必须点名。"""
    row = dict(_seed_rows()[0])
    where = patch.pop("_where")
    if where == "row":
        key = patch.pop("_key")
        row[key] = patch[key]
    else:
        row["conditions"] = [dict(row["conditions"][0], **patch)]
    rules, rejected = rulebook_from_rows([row])
    assert rules == []
    assert len(rejected) == 1 and rejected[0].startswith("第 1 行 R-DEBRIS-RAIN-1")


def test_坏行不影响好行装载() -> None:
    good = _seed_rows()[0]
    bad = dict(_seed_rows()[1], triggered_level=0)
    other = dict(_seed_rows()[7])
    rules, rejected = rulebook_from_rows([good, bad, other])
    assert [rule.rule_id for rule in rules] == ["R-DEBRIS-RAIN-1", "R-LAKE-1"]
    assert len(rejected) == 1


# ---------- 换版与回滚 ----------


def test_换版后判据与定级同时跟着变() -> None:
    """回归风险点：RiskEngine 缓存了 rule_id→等级映射，换版不刷新就会按旧阈值定级。"""
    engine = RuleEngine()
    risk = RiskEngine(engine)
    original = engine.rule_by_id("R-DEBRIS-RAIN-1")
    assert original is not None

    engine.reload([replace(rule, version=2) for rule in engine.rules], source="postgres")
    assert engine.source == "postgres"
    assert risk.level_of("R-DEBRIS-RAIN-1") is RiskLevel.ORANGE
    assert all(rule.version == 2 for rule in engine.rules)

    raised = replace(
        original,
        conditions=(Condition("rain_10min", ">=", 999.0, "max", 3600),),
        version=3,
        triggered_level=RiskLevel.YELLOW,
    )
    engine.reload([raised if rule.rule_id == raised.rule_id else rule for rule in engine.rules], source="postgres")
    # 阈值抬到 999 之后，35mm 的短时强降雨不再命中该规则：定级映射也必须是新版的黄
    assert risk.level_of("R-DEBRIS-RAIN-1") is RiskLevel.YELLOW
    assert engine.rule_by_id("R-DEBRIS-RAIN-1") is not None and engine.rule_by_id("R-DEBRIS-RAIN-1").version == 3


def test_回滚就是把上一版重新置为生效() -> None:
    engine = RuleEngine()
    first = list(engine.rules)
    v2 = [replace(rule, version=2) for rule in first]
    engine.reload(v2, source="postgres")
    engine.reload(first, source="builtin-rollback")
    assert [rule.version for rule in engine.rules] == [1] * len(first)
    assert engine.source == "builtin-rollback"


def test_空规则集与非active版本一律拒绝装载() -> None:
    engine = RuleEngine()
    before = engine.rules
    with pytest.raises(ValueError, match="为空"):
        engine.reload([], source="postgres")
    with pytest.raises(ValueError, match="非 active"):
        engine.reload([replace(rule, status="draft") for rule in before], source="postgres")
    with pytest.raises(ValueError, match="多个版本"):
        engine.reload([before[0], before[0]], source="postgres")
    assert engine.rules == before, "拒绝装载之后必须还是原来那一版"


def test_描述面外显出处与未标定计数() -> None:
    assert RuleEngine().describe()["source"] == "builtin"
    described = RuleEngine(default_rulebook()).describe()
    assert described["source"] == "injected", "显式传入的规则集不该冒充内置种子"
    assert described["rules"] == 9
    assert described["versions"]["R-LAKE-2"] == 1
    assert HazardType(described["hazards"][0]) in set(HazardType)
