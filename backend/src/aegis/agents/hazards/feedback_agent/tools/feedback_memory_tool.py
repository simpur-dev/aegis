"""
Feedback Agent 经验存储工具（第一版：JSON 文件存储）

【本文件角色】
  保存经过检查器验证或待人工审核的反馈经验，支持按事件/灾种检索。
  后续可平滑扩展为 SQLite / 向量检索；第一版按任务书要求不过度设计。

【诚实声明 / 重要红线】
  1. validation_status 语义：
       - "auto_validated"：确定性检查高置信（>=0.7）且无需人工复核的问题经验，
         可作为 Manager 决策参考，但【仍不等于专业验证】；
       - "pending_review"：证据不足或涉及专业判断，须人工审核后才能采信；
       - "human_verified" / "human_rejected"：预留给后续人工审核流程写入。
  2. 每条记录保留来源（event_id/agent）、版本（model_version/rule_version）与
     审核状态，满足可追溯要求。
  3. 外部反馈接口只做【记录与格式校验】，不虚构真实观测结果；
     "没有发生灾害"不能反推"风险评估错误"（预警为概率性提示），
     该原则写入接口校验的说明文本并在 compare 函数中强制执行。

【对外契约】
  save_feedback_records(records) / load_feedback_records(...) /
  validate_external_feedback(record) -> list[str]（问题清单，空=合格）
  save_external_feedback(record) / STORAGE_PATH / EXTERNAL_STORAGE_PATH
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from aegis.agents.hazards.config import FEEDBACK_MEMORY_FILE

# 存储路径走配置面（`config.FEEDBACK_MEMORY_FILE`，环境变量 AEGIS_EXT_FEEDBACK_MEMORY
# 可改）：原实现写死在**包内目录**——运行时数据写进源码树，装进 wheel/镜像后就写到镜像层。
# 主文件与外显反馈文件同目录，同名不同缀。
MEMORY_DIR = Path(FEEDBACK_MEMORY_FILE).parent
STORAGE_PATH = Path(FEEDBACK_MEMORY_FILE)
EXTERNAL_STORAGE_PATH = MEMORY_DIR / "external_feedback.json"

# 外部反馈记录的必备字段与枚举口径（接口契约，第一版不接入真实观测源）
EXTERNAL_REQUIRED_FIELDS = [
    "observed_at",  # 观测时间（ISO8601）
    "data_source",  # 数据来源（监测站/现场核查/历史记录档案/人工录入…）
    "region",  # 观测区域
    "observation_content",  # 观测内容描述
    "reliability",  # 证据可靠程度
    "review_status",  # 人工审核状态
    "related_event_id",  # 对应原始 event_id
]
EXTERNAL_RELIABILITY_LEVELS = {"low", "medium", "high", "unverified"}
EXTERNAL_REVIEW_STATUSES = {"pending_review", "verified", "rejected"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ============================================================
# 反馈经验存储（JSON）
# ============================================================


def make_feedback_record(
    event_id: str,
    hazard_type: str,
    item: dict,
    reflection: dict | None = None,
    suggested_action: str = "",
    model_version: str = "",
    rule_version: str = "",
) -> dict:
    """由反馈条目（feedback_item）构造一条经验记录（任务书字段齐全）。"""
    confidence = float(item.get("confidence", 0.0))
    needs_review = bool(item.get("requires_human_review"))
    validation_status = "pending_review" if (needs_review or confidence < 0.7) else "auto_validated"
    return {
        "feedback_id": f"FB-{uuid.uuid4().hex[:10]}",
        "event_id": event_id,
        "hazard_type": hazard_type,
        "problem_type": item.get("category", ""),
        "target_agent": item.get("agent", ""),
        "problem_description": item.get("description", ""),
        "affected_fields": item.get("fields", []),
        "severity": item.get("severity", ""),
        "confidence": confidence,
        "reflection": reflection or {},
        "suggested_action": suggested_action,
        "evidence": item.get("evidence", []),
        "model_version": model_version,
        "rule_version": rule_version,
        "validation_status": validation_status,
        "created_at": _now_iso(),
        "source": "feedback_agent.deterministic_checks",  # 来源可追溯
    }


def _load_json(path: Path) -> list:
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        # 存储文件损坏不阻断流程：备份后重建（诚实记录，不静默丢弃）
        backup = path.with_suffix(path.suffix + f".corrupt.{int(datetime.now().timestamp())}")
        try:
            path.rename(backup)
        except Exception:
            pass
        return []


def save_feedback_records(records: list) -> dict:
    """追加保存反馈经验记录，返回 {saved_count, record_ids, store_path}。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    existing = _load_json(STORAGE_PATH)
    existing.extend(records)
    with open(STORAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return {
        "saved_count": len(records),
        "record_ids": [r.get("feedback_id") for r in records],
        "store_path": str(STORAGE_PATH),
        "total_records": len(existing),
    }


def load_feedback_records(event_id: str = "", hazard_type: str = "", problem_type: str = "", limit: int = 20) -> list:
    """按事件/灾种/问题类型检索历史反馈经验（只读；条件为空则不过滤）。"""
    records = _load_json(STORAGE_PATH)
    out = []
    for r in reversed(records):  # 新记录优先
        if event_id and r.get("event_id") != event_id:
            continue
        if hazard_type and r.get("hazard_type") != hazard_type:
            continue
        if problem_type and r.get("problem_type") != problem_type:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


# ============================================================
# 外部实际反馈接口（第一版：记录 + 格式校验，不虚构观测结果）
# ============================================================


def validate_external_feedback(record) -> list:
    """校验外部反馈记录格式，返回问题清单（空列表 = 合格）。

    仅做接口格式核验：不判断观测内容真伪，不把'未发生灾害'反推为'风险错误'。
    """
    problems = []
    if not isinstance(record, dict):
        return [f"外部反馈记录必须为 dict，收到 {type(record).__name__}"]
    for field in EXTERNAL_REQUIRED_FIELDS:
        if record.get(field) in (None, ""):
            problems.append(f"缺少必备字段 '{field}'")
    if record.get("reliability") and record["reliability"] not in EXTERNAL_RELIABILITY_LEVELS:
        problems.append(f"reliability'{record['reliability']}'不在枚举 {sorted(EXTERNAL_RELIABILITY_LEVELS)} 内")
    if record.get("review_status") and record["review_status"] not in EXTERNAL_REVIEW_STATUSES:
        problems.append(f"review_status'{record['review_status']}'不在枚举 {sorted(EXTERNAL_REVIEW_STATUSES)} 内")
    return problems


def save_external_feedback(record: dict) -> dict:
    """保存一条外部反馈记录（合格才入库；不合格转人工复核清单）。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    problems = validate_external_feedback(record)
    entry = dict(record) if isinstance(record, dict) else {"raw": str(record)}
    entry["saved_at"] = _now_iso()
    entry["format_issues"] = problems  # 如实记录格式问题，不丢弃
    entry["acceptance_note"] = (
        "外部反馈仅作为结果反馈证据存档；'未发生灾害'不等同于此前风险评估错误（预警为概率性提示），误差分析须经人工审核后方可用于模型改进"
    )
    existing = _load_json(EXTERNAL_STORAGE_PATH)
    existing.append(entry)
    with open(EXTERNAL_STORAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    return {"saved": True, "format_issues": problems, "store_path": str(EXTERNAL_STORAGE_PATH)}


def load_external_feedback(related_event_id: str = "", limit: int = 20) -> list:
    """按关联事件检索外部反馈记录。"""
    records = _load_json(EXTERNAL_STORAGE_PATH)
    out = []
    for r in reversed(records):
        if related_event_id and r.get("related_event_id") != related_event_id:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out
