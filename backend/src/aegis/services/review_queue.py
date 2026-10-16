"""低置信护栏的读者：把"智能体建议人工核签"的那些链路捞成一张待办。

修前的形状（§3.5 目标里点名的"只有生产者、没有读者"）：`human_review_suggested` 由 assess 与
perceive 产出，随后只以一句 `rationale` 文本活着（"（建议人工核签：置信度 …）"）。
文本不能被查询、不能被计数、不能变成待办——于是护栏报了，没人接。

这里只做**读与摊平**，不新增一套状态机：一条待办算不算"已消解"，看的是 `warning_truth_labels`
里有没有对应 `case_id` 的那一行（`case_id` 即 `event_id`，见 `services/label_flywheel.py`）。
那是 D9 人工改判的真表，再造一个"已处理"字段就等于给同一件事两套口径。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: 护栏这一格的名字：与智能体载荷、`chain_runs.risk` 完全同名，方便 grep 到出处。
REVIEW_FLAG = "human_review_suggested"


def _as_ledger_row(row: Any) -> Mapping[str, Any] | None:
    """把台账行归一成同一份 dict：内存读视图给 `ChainResult`，库侧给它的 `as_dict()` 结果。

    两条分支各抽一次字段会漂——`ChainResult` 上根本没有 `risk` 这个属性（`risk` 是
    `as_dict()` 里由 `verdict.as_payload()` 现算的），照 dict 的读法写就会在内存形态下
    **永远返回空清单**，而那看起来完全像"这一程没有人建议核签"。
    """
    if isinstance(row, Mapping):
        return row
    as_dict = getattr(row, "as_dict", None)
    if callable(as_dict):
        data = as_dict()
        return data if isinstance(data, Mapping) else None
    return None


def pending_items(rows: Any, *, region_code: str | None = None, labelled: set[str] | None = None) -> list[dict[str, Any]]:
    """筛出建议人工核签的那几条，带上人做判断需要的出处信息。

    `labelled` 传 None 表示"这台没接标注面，无法判断签没签过"——那一格就写 None，
    不等于 `False`：把"查不到"说成"没签过"，值班员会去重签一遍已经签过的。
    """
    items: list[dict[str, Any]] = []
    for raw in rows:
        row = _as_ledger_row(raw)
        if row is None:
            continue
        risk = row.get("risk")
        if not isinstance(risk, Mapping) or not risk.get(REVIEW_FLAG):
            continue
        region = str(risk.get("region_code") or "")
        if region_code and region != region_code:
            continue
        event_id = str(row.get("event_id") or "")
        items.append(
            {
                "trace_id": row.get("trace_id"),
                "event_id": event_id,
                "warning_id": row.get("warning_id"),
                "region_code": region,
                "risk_level": risk.get("risk_level"),
                "confidence": risk.get("confidence"),
                "rationale": str(risk.get("rationale") or "")[:500],
                "assessed_by": risk.get("assessed_by"),
                "finished_at": row.get("finished_at"),
                "already_reviewed": None if labelled is None else event_id in labelled,
            }
        )
    return items


def case_ids(items: list[dict[str, Any]]) -> list[str]:
    return [str(item["event_id"]) for item in items if item["event_id"]]
