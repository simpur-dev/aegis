"""研判置信度策略（生产方自产，平台只校验不代填）。

口径：**置信度是证据强度，不是概率**。它由两个可在结论里复核的事实推导：

- `coverage`：P/T 两组加权因子覆盖率的较小者（两组都覆盖到，结论才立得住）；
- `validation`：内部验证节点是否判"证据充分且一致"（0/1）。

公式：`min(0.60, 0.15 + 0.40×coverage + 0.05×validation)`。

三条硬性质（测试逐条钉住）：
1. 永不为 0：最低 0.15——"证据很弱"不等于"没有研判"；
2. 永不超 0.60：方法是 MOCK 因子引擎 + 原型候选公式，上限写死在方法里，
   不随数据齐全而升高（数据齐只能证明"喂饱了引擎"，不能证明"引擎能预警"）；
3. 随覆盖率单调，且 `basis` 明写四项，读的人能自己复算。
"""

from __future__ import annotations

import math

CONFIDENCE_FLOOR = 0.15
COVERAGE_WEIGHT = 0.40
VALIDATION_BONUS = 0.05
MOCK_CAP = 0.60
METHOD_TAG = "factor_engine_mock"


def assess_confidence(completeness: dict, validation: dict) -> tuple[float, str]:
    """由分组覆盖率与验证结果推导 (confidence, basis)。

    参数:
        completeness: 因子引擎信封的 `completeness`（P/T 组加权覆盖率，0..1）
        validation: 验证节点写出的 `validation_result`
    """
    numeric = [float(value) for value in (completeness or {}).values() if isinstance(value, int | float) and math.isfinite(value)]
    # 两组都在时取较小者（两块短板决定整桶水）；一组缺失就按已覆盖的那组算，不凭空补 0.5。
    coverage = max(0.0, min(1.0, min(numeric) if numeric else 0.0))
    passed = bool((validation or {}).get("is_valid") and (validation or {}).get("evidence_sufficient"))
    validation_score = 1.0 if passed else 0.0
    confidence = round(min(MOCK_CAP, CONFIDENCE_FLOOR + COVERAGE_WEIGHT * coverage + VALIDATION_BONUS * validation_score), 4)
    basis = f"coverage={coverage:.2f};validation={'pass' if passed else 'fail'};method={METHOD_TAG};cap={MOCK_CAP:.2f}"
    return confidence, basis


__all__ = ["CONFIDENCE_FLOOR", "MOCK_CAP", "assess_confidence"]
