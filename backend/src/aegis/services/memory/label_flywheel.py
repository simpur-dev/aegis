"""数据飞轮（D9 / §4.5）：人工核签的签字结果自动变成标注样本，喂给已有的真值表。

单一真源：标注的落地形态就是 `warning_truth_labels`（004 迁移）+ `persistence/replay.py` 那个
唯一裁判，本模块不新建表、不自算任何指标——只做"把人的判断翻译成一行真值"的映射与投递。

三条不许含糊的规矩：
- **不猜真值**：`adjust`（改判）必须带改写后的等级，缺等级就不产出标注（如实报"没沉淀"），
  而不是拿被改判的那条等级冒充人的结论；`reject` 才写 `truth_warning=false`。
- **落不下要说**：`store_backend=memory` 时没有标注面可写，返回可见的"未沉淀 + 原因"，
  并把这条计入 `stats`——静默丢弃就是"飞轮在转"的假象（接线之前正是如此：核签结果只活在内存实例里）。
- **幂等以事件为键**：同一事件的工单反复改判，后一次覆盖前一次（`ON CONFLICT (case_id)`），
  人的最后一句为准；不给同一事件攒出两条互相矛盾的真值。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aegis.domain.messages import parse_iso

log = logging.getLogger("aegis.labels")

SOURCE_REVIEW = "human_review"
# 工单里的这个标记由 `container._open_report_review` 打上：只有"对一条结论做人工裁决"的
# human_review 才进标注池；别的会签节点（演练放行等）不该被当成现场真值。
CAPTURE_MARKER = "report_review"


def label_from_review(*, instance_payload: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """把人的一票翻译成一行真值标注。字段名严格对齐 `accuracy.LABEL_COLUMNS`。"""
    if instance_payload.get("capture_label") != CAPTURE_MARKER:
        raise ValueError("该工单未标记为结论裁决，不进标注池")
    region = str(instance_payload.get("region_code") or "")
    hazard = str(instance_payload.get("hazard_type") or "")
    case_id = str(instance_payload.get("event_id") or "")
    raw_moment = str(instance_payload.get("observed_at") or "")
    if not region or not hazard or not case_id or not raw_moment:
        raise ValueError(
            f"工单缺少标注必需字段（region/hazard/event_id/observed_at 之一为空）：{region!r}/{hazard!r}/{case_id!r}/{raw_moment!r}"
        )
    # 落库面（`accuracy.normalize_label`）要的是带时区的 datetime 而不是字符串：
    # 在这里转一次，缺时区就地拒绝——裸时间会被按本地时区解释，整条真值轴平移几小时且没人看得出来。
    try:
        observed_at = parse_iso(raw_moment)
    except ValueError as exc:
        raise ValueError(f"工单的 observed_at 不是合法 ISO 8601：{raw_moment!r}") from exc
    if observed_at.tzinfo is None:
        raise ValueError(f"工单的 observed_at 缺时区，不能当作假定时刻的真值：{raw_moment!r}")
    choice = str(decision.get("choice") or "")
    reviewer = str(decision.get("by") or "unknown")
    if choice == "approve":
        truth_warning, truth_level = True, instance_payload.get("risk_level")
    elif choice == "reject":
        truth_warning, truth_level = False, None
    elif choice == "adjust":
        level = decision.get("level")
        if level is None:
            # 改判却没给改判后的等级：这时任何数值都是替人编的，宁可不落标注
            raise ValueError("改判(adjust)必须同时给出 level，否则无法确定真值等级")
        truth_warning, truth_level = True, int(level)
    else:
        raise ValueError(f"无法识别的裁决选项: {choice!r}")
    note = str(decision.get("comment") or "")[:512]
    return {
        "case_id": case_id,
        "region_code": region,
        "hazard_type": hazard,
        "observed_at": observed_at,
        "truth_warning": truth_warning,
        "truth_level": truth_level,
        "source": SOURCE_REVIEW,
        "labelled_by": reviewer,
        "note": note,
    }


class LabelFlywheel:
    """把标注投递到存储的标注面；投递不到就如实计数并说明原因。"""

    def __init__(self, store: Any | None) -> None:
        self._store = store
        self.stats: dict[str, int] = {"已沉淀": 0, "未沉淀": 0, "映射失败": 0}

    async def record_review(self, *, instance_payload: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
        """返回一行可 JSON 序列化的事实，供接口直接回给签字的人。"""
        try:
            label = label_from_review(instance_payload=instance_payload, decision=decision)
        except ValueError as exc:
            self.stats["映射失败"] += 1
            return {"status": "未沉淀", "原因": str(exc)}

        writer = getattr(self._store, "put_warning_labels", None)
        if writer is None:
            self.stats["未沉淀"] += 1
            return {
                "status": "未沉淀",
                "原因": "当前存储视图没有标注面（store_backend=memory）；换成 postgres 才会落库",
                "样本": echo_label(label),
            }
        try:
            await writer([label])
        except Exception as exc:  # 标注落库失败不能反噬核签本身：如实报"未沉淀"，不抛出
            self.stats["未沉淀"] += 1
            log.warning("标注写入失败", extra={"err": type(exc).__name__, "case_id": label["case_id"]})
            return {"status": "未沉淀", "原因": f"{type(exc).__name__}: {exc}", "样本": echo_label(label)}
        self.stats["已沉淀"] += 1
        return {"status": "已沉淀", "case_id": label["case_id"], "真值": f"报={label['truth_warning']} 等级={label['truth_level']}"}


def echo_label(label: dict[str, Any]) -> dict[str, Any]:
    """样本回显给签字的人看：`observed_at` 转回 ISO 串，接口出口保持 `json.dumps` 直接可写。"""
    moment = label["observed_at"]
    return {**label, "observed_at": moment.isoformat() if isinstance(moment, datetime) else moment}
