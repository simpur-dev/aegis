"""解析评测集与算式的门禁（批次 C4）。

这个文件要守的两件事：
1. 评测集本身够格（≥30 例、≥5 灾种、字段齐全、含"不该报警"的负样本）——
   否则"识别 ≥5 类触发条件"这句话没有分母；
2. 规则/词表腿的真实准确率作为回归基线钉住。数字来自实测，不来自期望：
   用例里刻意放了干扰数字、单位混淆、多灾种词共现与无区划码的样本，判错就红。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aegis.domain.enums import HazardType
from aegis.services.parsing_eval import (
    EvalDatasetError,
    evaluate,
    lexical_baseline,
    load_dataset,
    parse_case,
    score_case,
)
from aegis.services.semantic_parser import ParsedDisaster

DATASET = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "report_parsing_cases.jsonl"


@pytest.fixture(scope="module")
def dataset() -> Any:
    return load_dataset(DATASET)


def test_评测集规模与灾种覆盖撑得起指标口径(dataset: Any) -> None:
    assert len(dataset.cases) >= 30
    hazards = {case.hazard_type for case in dataset.cases}
    assert len(hazards) >= 5, hazards
    assert dataset.kind == "synthetic", "回归集必须自报合成性质，否则会被当成现场准确率证据"
    assert all(case.text.strip() for case in dataset.cases)


def test_评测集含不该报警的负样本(dataset: Any) -> None:
    """只有正样本的评测集会奖励"什么都报"，那比漏报更糟。"""
    negatives = [case for case in dataset.cases if case.risk_level is None]
    assert len(negatives) >= 6, f"负样本只有 {len(negatives)} 条"
    assert any(case.region_code is None for case in dataset.cases), "缺区划码这类真实现场缺陷必须进集"


def test_解析一行用例的字段口径() -> None:
    case = parse_case({"id": "X-1", "text": "540121 泥石流", "hazard_type": "debris_flow", "metrics": {"rain_10min": 30}}, line_no=1)
    assert case.expects_warning is False
    assert case.metrics == {"rain_10min": 30.0}
    with pytest.raises(EvalDatasetError, match="缺字段"):
        parse_case({"text": "只有文本"}, line_no=2)
    with pytest.raises(EvalDatasetError, match="metrics"):
        parse_case({"id": "X", "text": "t", "hazard_type": "unknown", "metrics": [1, 2]}, line_no=3)


def test_数据集错误可见(tmp_path: Path) -> None:
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(EvalDatasetError, match="不存在"):
        load_dataset(missing)
    duplicated = tmp_path / "dup.jsonl"
    duplicated.write_text(
        '{"id":"A","text":"x","hazard_type":"unknown"}\n{"id":"A","text":"y","hazard_type":"unknown"}\n', encoding="utf-8"
    )
    with pytest.raises(EvalDatasetError, match="重复"):
        load_dataset(duplicated)


def test_判分对空等级的语义是不该报才算对() -> None:
    case = parse_case({"id": "N-1", "text": "540121 有裂缝", "hazard_type": "landslide", "risk_level": None}, line_no=1)
    quiet = ParsedDisaster(
        text=case.text,
        hazard_type=HazardType.LANDSLIDE,
        region_code="540121",
        risk_level=None,
        confidence=0.0,
        decided_by="none",
    )
    loud = ParsedDisaster(
        text=case.text,
        hazard_type=HazardType.LANDSLIDE,
        region_code="540121",
        risk_level=1,
        confidence=0.9,
        decided_by="rule",
        metrics=(("displacement_mm", 30.0, "mm"),),
    )
    assert score_case(case, quiet).level_correct is True
    assert score_case(case, loud).level_correct is False
    assert score_case(case, loud).metrics_expected == 0
    assert score_case(case, loud).metrics_spurious == 1


#: 本机实跑基线（2026-10-03，33 例，只量确定性腿）：
#: hazard 0.9697 / region 1.0 / metric precision 0.9762 / recall 0.9762 / level 0.9091。
#: 阈值取"当前水平减一档"，回归即红；已知判错的用例逐条列在下面，每条都是一个可解释的边界。
KNOWN_HAZARD_MISSES = {"RPT-25"}
KNOWN_LEVEL_MISSES = {"RPT-08", "RPT-26", "RPT-32"}
KNOWN_METRIC_GAPS: set[str] = {"RPT-32"}  # 同一条文本里"从 12 毫米扩大到 20 毫米"与"冻融循环 5 次"竞争，绑定距离内两边只能认一个


@pytest.mark.asyncio
async def test_确定性腿的实测准确率守住基线(dataset: Any) -> None:
    report = await evaluate(dataset)
    assert report.cases >= 30
    assert report.hazard_accuracy >= 0.95, report.as_dict()["by_hazard"]
    assert report.region_accuracy >= 0.99
    assert report.metric_recall >= 0.95, [row.case_id for row in report.outcomes if row.metrics_found < row.metrics_expected]
    assert report.metric_precision >= 0.95, [row.case_id for row in report.outcomes if row.metrics_spurious]
    assert report.level_accuracy >= 0.85
    assert report.official_claim is False, "合成集不得声称官方达成"
    assert {"rule", "rule_declared", "none"} & {row.decided_by for row in report.outcomes}


@pytest.mark.asyncio
async def test_已知判错的用例可枚举而不是悄悄算进成绩(dataset: Any) -> None:
    """把"哪儿还判不对"钉成清单：词表补上了清单该缩，判错了新增用例该红——两边都不许含糊。"""
    report = await evaluate(dataset)
    hazard_misses = {row.case_id for row in report.outcomes if not row.hazard_correct}
    level_misses = {row.case_id for row in report.outcomes if not row.level_correct}
    metric_misses = {row.case_id for row in report.outcomes if row.metrics_found < row.metrics_expected or row.metrics_spurious}
    assert hazard_misses <= KNOWN_HAZARD_MISSES, hazard_misses - KNOWN_HAZARD_MISSES
    assert level_misses <= KNOWN_LEVEL_MISSES, level_misses - KNOWN_LEVEL_MISSES
    assert metric_misses <= KNOWN_METRIC_GAPS, metric_misses - KNOWN_METRIC_GAPS


def test_词表基线可单独对照(dataset: Any) -> None:
    """关掉整条解析链也能单独量词表：标定实验要能回答"LLM 腿到底加了多少"。"""
    baseline = lexical_baseline(dataset.cases[0].text)
    assert baseline["hazard_type"] == "debris_flow"
    assert baseline["region_code"] == dataset.cases[0].region_code
    assert baseline["metrics"]["rain_cumulative_24h"] == 95.0
    assert baseline["risk_level"] == 1 or baseline["risk_level"] is None


@pytest.mark.asyncio
async def test_不带llm时解析器不会被装配(dataset: Any) -> None:
    """`with_llm=False` 必须真的不接裁决腿：否则 CI 会在没有密钥的机器上静默走异常降级路径。"""
    report = await evaluate(dataset, with_llm=False)
    assert "仅规则" in report.note
    assert all(row.decided_by != "llm" for row in report.outcomes), [row.case_id for row in report.outcomes if row.decided_by == "llm"]
