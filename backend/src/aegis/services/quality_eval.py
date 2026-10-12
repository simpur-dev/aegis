"""质量表与回归门禁（§4.5 / D8、§5）：CI 合入前的"不退步"闸门。

表 = 触发F1 / 定级一致率 / STU合法率 / 文案裁判分 / 结构化输出首过率。本模块只做**确定性可验证**的部分：
- STU合法率：直接复用 `ContractRegistry.stu_errors`（真实计算，非声称）；
- 结构化输出首过率：从网关用量台账算（台账每格都由真调用落笔，非事后填数）；
- 回归门禁：给定当前与基线指标，逐列比对、任一回退即判不过；
- 诚实留白：某列**未评测**（None）不参与"通过"、也不算"退步"——只有真跑出的数才能进基线比较。
  触发F1/定级一致率/文案裁判分需金标集与裁判模型（数据/密钥门），此处只提供计算与闸门口，
  不臆造任何质量数字（对齐全平台"数字须实测"纪律）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aegis.bus.gateway import ContractRegistry


def stu_legality_rate(task_units: list[dict[str, Any]], contracts: ContractRegistry) -> tuple[float, int]:
    """(合法率, 违规条数)。空输入按 1.0/0（没有产出不等于有非法产出，但也不声称"达标"）。"""
    errors = contracts.stu_errors(task_units)
    total = len(task_units)
    if total == 0:
        return 1.0, 0
    bad = len({e["index"] for e in errors})
    return round((total - bad) / total, 4), bad


def structured_first_pass_rate(rows: Sequence[dict[str, Any]]) -> tuple[float | None, dict[str, int]]:
    """结构化输出首过率（§4.1.2/§8）：每次结构化调用的**首答**（attempt==0）就通过 schema 校验的占比。

    输入是网关用量台账的 `as_dict()` 行（唯一形状；不做对象/字典双兼容，免得读错档形却静默算成空）。

    分母只算"模型真的答了"的行（`ok`/`malformed`）；`transport_error`/`unavailable` 另计数不进分母——
    那是网关故障，混进来会把"连不上模型"读成"模型格式能力退步"，这两件事必须分开看。
    一条可算的首答都没有 → None（未评测）：绝不因为"没数"就冒充满分，也不因为"没数"就谎报退步。
    """
    first_ok = need_repair = not_answered = 0
    for row in rows:
        if not row.get("structured") or int(row.get("attempt", 0)) != 0:
            continue
        validation = str(row.get("validation", ""))
        if validation == "ok":
            first_ok += 1
        elif validation == "malformed":
            need_repair += 1
        else:
            not_answered += 1
    answered = first_ok + need_repair
    breakdown = {"首过": first_ok, "需修复": need_repair, "未答(传输或熔断)": not_answered}
    if answered == 0:
        return None, breakdown
    return round(first_ok / answered, 4), breakdown


@dataclass(slots=True)
class QualityMetrics:
    """质量表当前值；None 表示该列"未评测"，绝不以 0 冒充。"""

    trigger_f1: float | None = None
    level_agreement: float | None = None
    stu_legality: float | None = None
    copy_judge_score: float | None = None
    structured_first_pass: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "trigger_f1": self.trigger_f1,
            "level_agreement": self.level_agreement,
            "stu_legality": self.stu_legality,
            "copy_judge_score": self.copy_judge_score,
            "structured_first_pass": self.structured_first_pass,
        }


@dataclass(frozen=True, slots=True)
class GateResult:
    passed: bool
    regressions: list[dict[str, Any]]
    unmeasured: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "regressions": self.regressions, "unmeasured": self.unmeasured}


#: 允许的回退容差（同单位 0..1）；默认严格（>0 即退步）。是否放宽须由现场显式决定。
_DEFAULT_TOLERANCE = 0.0


def regression_gate(current: QualityMetrics, baseline: QualityMetrics, *, tolerance: float = _DEFAULT_TOLERANCE) -> GateResult:
    """逐列比对当前 vs 基线：任一回退超过容差即不过。当前未测(None)不判过也不判退，记入 unmeasured。"""
    rows = {
        "trigger_f1": (current.trigger_f1, baseline.trigger_f1),
        "level_agreement": (current.level_agreement, baseline.level_agreement),
        "stu_legality": (current.stu_legality, baseline.stu_legality),
        "copy_judge_score": (current.copy_judge_score, baseline.copy_judge_score),
        "structured_first_pass": (current.structured_first_pass, baseline.structured_first_pass),
    }
    regressions: list[dict[str, Any]] = []
    unmeasured: list[str] = []
    for name, (now, base) in rows.items():
        if now is None:
            unmeasured.append(name)
            continue
        if base is not None and now < base - tolerance:
            regressions.append({"metric": name, "current": now, "baseline": base, "delta": round(now - base, 4)})
    return GateResult(passed=not regressions, regressions=regressions, unmeasured=unmeasured)
