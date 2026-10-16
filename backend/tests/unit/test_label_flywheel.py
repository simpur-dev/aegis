"""数据飞轮（D9 / §4.5）：人工核签的那一票要变成 `warning_truth_labels` 里的一行真值。

这个模块的风险不在"有没有接线"，而在"接了线却一行都没落进真值表"，所以这里钉三处：
- **改判不给等级时不许产标注**：拿被改判那条的等级冒充人的结论，比不落地更糟——真值表里
  从此多一条没人签过的数字，而它下游就是"预警准确率"；
- **内存存储没有标注面时必须如实回"未沉淀 + 原因"**：静默丢弃的症状是"飞轮在转"的假象，
  现场看到的仍是零标注；
- **投递的字段集与 `accuracy.LABEL_COLUMNS` 逐一对齐**：`normalize_label` 要的是带时区的
  `datetime`，用字符串过关会在真栈落库那一刻才炸——所以这里直接拿真裁判函数验形状，
  而不是只对着假 store 比字典。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from aegis.persistence.accuracy import LABEL_COLUMNS, normalize_label
from aegis.services.memory.label_flywheel import CAPTURE_MARKER, LabelFlywheel, label_from_review

OBSERVED = "2026-10-11T03:21:44.500Z"


def _ticket(**overrides: Any) -> dict[str, Any]:
    """`container._open_report_review` 开出的工单形状（字段名必须与那边逐字相同）。"""
    payload: dict[str, Any] = {
        "capture_label": CAPTURE_MARKER,
        "observed_at": OBSERVED,
        "region_code": "540200",
        "hazard_type": "debris_flow",
        "risk_level": 3,
        "event_id": "evt_9c1",
        "note": "发生泥石流，请求红色预警",
    }
    payload.update(overrides)
    return payload


def _decision(choice: str = "approve", **overrides: Any) -> dict[str, Any]:
    decision: dict[str, Any] = {"choice": choice, "by": "值班员扎西", "comment": "", "level": None}
    decision.update(overrides)
    return decision


class _FakeStore:
    """只实现"有标注面"这一项能力，好让投递路径被真走一遍。"""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls = 0
        self._fail = fail

    async def put_warning_labels(self, rows: Any) -> int:
        self.calls += 1
        if self._fail is not None:
            raise self._fail
        self.rows.extend(rows)
        return len(rows)


class _MemoryStore:
    """纯内存读视图：没有任何标注面——这正是当前默认装配的形态。"""


def test_批准把工单里的等级原样成真值() -> None:
    label = label_from_review(instance_payload=_ticket(), decision=_decision("approve"))
    assert label["truth_warning"] is True
    assert label["truth_level"] == 3, "批准就是把机器给的等级确认为真值，不是重新算一遍"
    assert label["case_id"] == "evt_9c1"
    assert label["source"] == "human_review"
    assert label["labelled_by"] == "值班员扎西"
    assert isinstance(label["observed_at"], datetime) and label["observed_at"].tzinfo is not None


def test_否决写真值不报并且不编等级() -> None:
    label = label_from_review(instance_payload=_ticket(), decision=_decision("reject"))
    assert label["truth_warning"] is False, "否决就是当时不该报——误报/漏报的口径全靠这一票"
    assert label["truth_level"] is None


def test_改判按人给的等级落() -> None:
    label = label_from_review(instance_payload=_ticket(risk_level=3), decision=_decision("adjust", level=5))
    assert (label["truth_warning"], label["truth_level"]) == (True, 5), "改判到红色却落 3 级，等于把这一笔改判抹掉了"


def test_改判缺等级就拒绝产出() -> None:
    """缺 level 的改判必须整个拒绝：这时任何等级值都是替现场编的。"""
    with pytest.raises(ValueError, match="level"):
        label_from_review(instance_payload=_ticket(risk_level=3), decision=_decision("adjust"))


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"capture_label": None}, "未标记"),
        ({"hazard_type": None}, "必需字段"),
        ({"region_code": ""}, "必需字段"),
        ({"event_id": None}, "必需字段"),
        ({"observed_at": ""}, "必需字段"),
        # 裸时间与坏格式：缺时区的真值会让整条判定轴平移几小时且看不出来，宁可不落
        ({"observed_at": "2026-10-11T03:21:44.500"}, "缺时区"),
        ({"observed_at": "昨天早上"}, "ISO"),
    ],
    ids=["无标记", "缺灾种", "缺区域", "缺事件", "缺时刻", "裸时间", "坏时间"],
)
def test_不合格的工单一律拒绝进池(payload: dict[str, Any], reason: str) -> None:
    ticket = dict(_ticket())
    for key, value in payload.items():
        if value is None:
            ticket.pop(key)
        else:
            ticket[key] = value
    with pytest.raises(ValueError, match=reason):
        label_from_review(instance_payload=ticket, decision=_decision())


def test_认不出的裁决选项不产标注() -> None:
    with pytest.raises(ValueError, match="无法识别"):
        label_from_review(instance_payload=_ticket(), decision=_decision("maybe"))


def test_备注过长只截断不报错() -> None:
    label = label_from_review(instance_payload=_ticket(), decision=_decision("approve", comment="判据复核" * 300))
    assert len(label["note"]) == 512, "真值表 note 列有宽度上限，超长必须在这里收口"


def test_投递形状能被真裁判函数接受() -> None:
    """这条是本模块与持久层唯一的接缝：字段名/类型对不上，真栈上就是一行都写不进去。"""
    label = label_from_review(instance_payload=_ticket(), decision=_decision("approve"))
    assert set(label) == set(LABEL_COLUMNS)
    normalized = normalize_label(label)
    assert normalized["case_id"] == "evt_9c1"
    assert normalized["observed_at"] == label["observed_at"], "时刻已解析成 datetime，落库面不该再兜一次"
    assert normalized["truth_level"] == 3


@pytest.mark.asyncio
async def test_有标注面就投递一行并计入已沉淀() -> None:
    store = _FakeStore()
    flywheel = LabelFlywheel(store)
    result = await flywheel.record_review(instance_payload=_ticket(), decision=_decision("adjust", level=4))
    assert result["status"] == "已沉淀"
    assert store.calls == 1 and len(store.rows) == 1
    assert store.rows[0]["truth_level"] == 4
    assert flywheel.stats == {"已沉淀": 1, "未沉淀": 0, "映射失败": 0}


@pytest.mark.asyncio
async def test_内存存储下如实报未沉淀并带上样本() -> None:
    """默认装配就是这个形态：飞轮必须说自己没转，而不是返回一个看着像成功的字典。"""
    flywheel = LabelFlywheel(_MemoryStore())
    result = await flywheel.record_review(instance_payload=_ticket(), decision=_decision("approve"))
    assert result["status"] == "未沉淀"
    assert "memory" in result["原因"] and "postgres" in result["原因"]
    assert result["样本"]["case_id"] == "evt_9c1"
    assert isinstance(result["样本"]["observed_at"], str), "datetime 只留在投递面，接口出口给字符串"
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result


@pytest.mark.asyncio
async def test_落库失败不反噬核签本身() -> None:
    """签字已经生效了，标注写不进去不能把这单变成 500。"""
    store = _FakeStore(fail=RuntimeError("库断了"))
    flywheel = LabelFlywheel(store)
    result = await flywheel.record_review(instance_payload=_ticket(), decision=_decision("approve"))
    assert result["status"] == "未沉淀"
    assert "RuntimeError" in result["原因"] and "库断了" in result["原因"]
    assert flywheel.stats == {"已沉淀": 0, "未沉淀": 1, "映射失败": 0}


@pytest.mark.asyncio
async def test_映射失败根本不碰存储面() -> None:
    store = _FakeStore()
    flywheel = LabelFlywheel(store)
    result = await flywheel.record_review(instance_payload=_ticket(), decision=_decision("adjust"))
    assert result["status"] == "未沉淀" and "level" in result["原因"]
    assert "样本" not in result, "翻译都没过，就没有一行真值可示"
    assert store.calls == 0, "缺字段还往库里发，是在给真值表添噪"
    assert flywheel.stats == {"已沉淀": 0, "未沉淀": 0, "映射失败": 1}


@pytest.mark.asyncio
async def test_同一事件反复改判仍指向同一个真值行() -> None:
    """幂等键由事件决定而不是每次新生成：否则 `ON CONFLICT (case_id)` 永远撞不上，
    同一事件会攒出两条互相矛盾的真值，回放时谁在骗人说不清。"""
    store = _FakeStore()
    flywheel = LabelFlywheel(store)
    first = await flywheel.record_review(instance_payload=_ticket(), decision=_decision("approve"))
    second = await flywheel.record_review(instance_payload=_ticket(), decision=_decision("adjust", level=5))
    assert first["case_id"] == second["case_id"] == "evt_9c1"
    assert store.rows[-1]["truth_level"] == 5, "人的最后一句为准"


@pytest.mark.asyncio
async def test_存储面缺失时回显的样本仍是真值表的列() -> None:
    flywheel = LabelFlywheel(None)
    result = await flywheel.record_review(instance_payload=_ticket(), decision=_decision("reject"))
    assert set(json.loads(json.dumps(result, ensure_ascii=False))["样本"]) == set(LABEL_COLUMNS)
