"""因子权重校准骨架：现场真值 → 各组候选权重（或如实的 `not_measured`）。

**它解决的是哪一个未清项**：五个灾种风险工具的因子权重全部是
`weight_source.method=mock`（原型自拟，validation_status=mock_assumption）。
真标定需要**现场真值标注集**（`warning_truth_labels`：每次事件"到底发生没有"），
没有标注集就不可能有诚实的权重——本骨架把"有了标注之后怎么算"先立起来，
**没有标注时输出 `not_measured`，绝不拿 MOCK 数字冒充标定结果**。

**方法（透明、可审计，不上黑箱）**：`truth_separation_v0`——对每个因子，
比较"真发警报（truth_warning=true）"与"真未发（false）"两组案例里该因子的
归一化均值差，差越大区分度越高；组内区分度归一成候选权重。
它不是最优解，是**第一根标尺**：够透明，评审人能逐因子核对。

**为什么分组别算**：因子引擎的公式是 `R = 100 × P × T`（组内权重、组间相乘），
组内和恒为 1 才有"这个因子的相对重要性"可言。

**纪律（与 `services/rules/calibration.py` 的规则建议同一套）**：
- 输出**永远 `applied=false`**——候选权重要以新版本写进 `FACTOR_CONFIG` 必须走
  人工评审；这条闭环里没有任何自动改权重的路径，这是设计而不是待办；
- 样本闸：一个灾种的配对案例不足 `MIN_PAIRS` → 整灾种 `not_measured`；
  一个因子的案例出现率不足 `MIN_FACTOR_PRESENCE` → 不参与该组候选
  （拿两三个案例算出来的"区分度"是噪声）。

**输入**（都是离线文件，骨架不连库）：
- `--labels <jsonl>`：`warning_truth_labels` 导出（格式同
  `persistence/accuracy.read_label_file`：可选 `{"dataset":{...}}` 头行 + 标注行）；
- `--observations <jsonl>`：逐案例的因子观测（`assess.v1` 证据包导出）：
  `{"case_id", "hazard_type", "observations": [{"factor", "mean_value", ...}]}`。
  **今天还没有这条导出路径**（证据包活在请求载荷里，未落库）——这是骨架的
  如实前提：现在跑，预期输出就是 `not_measured`，理由会写明缺哪份输入。

用法：
    python scripts/calibrate_factor_weights.py                     # 全 not_measured 报告
    python scripts/calibrate_factor_weights.py --labels truth.jsonl --observations obs.jsonl
    python scripts/calibrate_factor_weights.py --labels ... --observations ... --out reports/weight_calibration
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aegis.agents.hazards.risk_agent.tools.avalanche_risk_tool import FACTOR_CONFIG as AVALANCHE_CONFIG
from aegis.agents.hazards.risk_agent.tools.debris_flow_risk_tool import FACTOR_CONFIG as DEBRIS_CONFIG
from aegis.agents.hazards.risk_agent.tools.glacial_lake_risk_tool import FACTOR_CONFIG as LAKE_CONFIG
from aegis.agents.hazards.risk_agent.tools.landslide_risk_tool import FACTOR_CONFIG as LANDSLIDE_CONFIG
from aegis.agents.hazards.risk_agent.tools.rockfall_risk_tool import FACTOR_CONFIG as ROCKFALL_CONFIG
from aegis.persistence.accuracy import read_label_file

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "reports" / "weight_calibration"
METHOD = "truth_separation_v0"
#: 一个灾种允许下结论的最少配对案例数（真值 + 该案例的因子观测都对上）。
MIN_PAIRS = 30
#: 一个因子参与候选所需的最低案例出现率（配对案例里有多少比例带它的观测）。
MIN_FACTOR_PRESENCE = 0.6

CONFIGS = {
    "avalanche": AVALANCHE_CONFIG,
    "debris_flow": DEBRIS_CONFIG,
    "lake_outburst": LAKE_CONFIG,
    "landslide": LANDSLIDE_CONFIG,
    "rockfall": ROCKFALL_CONFIG,
}

GROUPS = ("P", "T", "I")


def _read_observations(path: Path | None) -> dict[str, dict[str, float]]:
    """逐案例因子观测：`{case_id: {factor: mean_value}}`。缺文件不是错——骨架的常态输入。"""
    if path is None or not Path(path).is_file():
        return {}
    rows: dict[str, dict[str, float]] = {}
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"观测文件第 {line_no} 行不是合法 JSON：{exc}") from exc
        case_id = str(raw.get("case_id") or "").strip()
        if not case_id:
            raise SystemExit(f"观测文件第 {line_no} 行缺 case_id")
        values: dict[str, float] = {}
        for observation in raw.get("observations") or []:
            factor = str(observation.get("factor") or "").strip()
            mean = observation.get("mean_value")
            if factor and isinstance(mean, int | float) and mean == mean:  # NaN 不是数
                values[factor] = float(mean)
        rows[case_id] = values
    return rows


def _factor_decl(config: dict[str, Any], factor: str) -> dict[str, Any] | None:
    """因子声明（P/T 各组同名因子的声明一致，对账门禁钉过）；没有声明就 None。"""
    for group in GROUPS:
        for declaration in config["groups"].get(group, []):
            if declaration["key"] == factor:
                return declaration
    return None


def _normalized(value: float, declaration: dict[str, Any] | None) -> float:
    """按引擎同式归一（区分度标尺必须与引擎实际用的 z 同尺度）。

    band_pos（带内=1、带外线性衰减）此前被当 linear_pos 错算——坡度 50° 引擎 z≈0.33、
    校准脚本算 0.75，区分度不是同一根尺子。"""
    if declaration is None:
        return max(0.0, min(1.0, value)) if 0.0 <= value <= 1.0 else 0.0
    norm = declaration.get("normalize") or {}
    method = norm.get("method", "linear_pos")
    if method == "class_map":
        mapping = norm.get("class_map") or {}
        return max(0.0, min(1.0, float(mapping.get(str(value), 0.0))))
    lower, upper = norm.get("L"), norm.get("U")
    if not (isinstance(lower, int | float) and isinstance(upper, int | float) and upper > lower):
        return max(0.0, min(1.0, value)) if 0.0 <= value <= 1.0 else 0.0
    z = (value - lower) / (upper - lower)
    if method == "band_pos":
        # 与引擎 normalize_value 同式（三段式）：带内 1，带外按 band 边缘向 L/U 线性衰减。
        # 此前把整个 [L,U] 当带内（x=22 引擎 0.200 / 校准 1.000），区分度是两把尺子。
        band_lo, band_hi = norm.get("band_lo"), norm.get("band_hi")
        if not (isinstance(band_lo, int | float) and isinstance(band_hi, int | float)):
            return max(0.0, min(1.0, z))
        band_lo, band_hi = float(band_lo), float(band_hi)
        if band_hi <= band_lo:
            return max(0.0, min(1.0, z))
        if band_lo <= value <= band_hi:
            return 1.0
        if value < band_lo:
            span = max(band_lo - lower, 1e-9)
            return max(0.0, min(1.0, (value - lower) / span))
        span = max(upper - band_hi, 1e-9)
        return max(0.0, min(1.0, (upper - value) / span))
    return max(0.0, min(1.0, z))


def _pair_cases(labels: list[dict[str, Any]], observations: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """真值 × 观测配对：`truth_warning` 决定正负组；观测缺失的案例不进（如实丢，不补）。"""
    paired: list[dict[str, Any]] = []
    for label in labels:
        case_id = str(label.get("case_id") or "")
        values = observations.get(case_id)
        if not case_id or values is None:
            continue
        paired.append(
            {
                "hazard_type": str(label.get("hazard_type") or ""),
                "truth_warning": label.get("truth_warning") is True,
                "values": values,
            }
        )
    return paired


def _separation(paired: list[dict[str, Any]], factor: str, declaration: dict[str, Any] | None) -> float:
    """|均值(正组) − 均值(负组)|（归一化后）；任一组为空 → 0（没有区分证据）。"""
    positives = [_normalized(item["values"][factor], declaration) for item in paired if item["truth_warning"] and factor in item["values"]]
    negatives = [
        _normalized(item["values"][factor], declaration) for item in paired if not item["truth_warning"] and factor in item["values"]
    ]
    if not positives or not negatives:
        return 0.0
    return abs(sum(positives) / len(positives) - sum(negatives) / len(negatives))


def calibrate(labels_path: Path | None, observations_path: Path | None) -> dict[str, Any]:
    """主算式：整份报告 = 输入盘点 + 每灾种 measured/not_measured。"""
    hazards: dict[str, Any] = {}
    labels: list[dict[str, Any]] = []
    label_note = "未提供 --labels"
    if labels_path is not None and Path(labels_path).is_file():
        try:
            labels, _kind, _source, note = read_label_file(labels_path)
            label_note = note or f"{len(labels)} 行标注"
        except Exception as exc:  # 标注文件的口径错误要带着原因如实进报告
            label_note = f"读取失败：{type(exc).__name__}: {exc}"
    observations = _read_observations(observations_path)
    paired = _pair_cases(labels, observations)

    for hazard, config in CONFIGS.items():
        cases = [item for item in paired if item["hazard_type"] == hazard]
        if not observations:
            hazards[hazard] = {
                "status": "not_measured",
                "reason": "缺因子观测导出（--observations）：证据包尚未落库，无从配对",
                "pairs": len(cases),
                "groups": {},
            }
            continue
        if len(cases) < MIN_PAIRS:
            hazards[hazard] = {
                "status": "not_measured",
                "reason": f"配对案例 {len(cases)} < 最小样本 {MIN_PAIRS}（少到这个量级的区分度是噪声）",
                "pairs": len(cases),
                "groups": {},
            }
            continue
        groups: dict[str, Any] = {}
        for group in GROUPS:
            declarations = config["groups"].get(group, [])
            presence_floor = MIN_FACTOR_PRESENCE * len(cases)
            candidates: dict[str, dict[str, Any]] = {}
            for declaration in declarations:
                factor = declaration["key"]
                hits = sum(1 for item in cases if factor in item["values"])
                if hits < presence_floor:
                    continue
                candidates[factor] = {
                    "discrimination": round(_separation(cases, factor, _factor_decl(config, factor)), 6),
                    "presence": hits,
                    "mock_weight": declaration["weight"],
                }
            total = sum(item["discrimination"] for item in candidates.values())
            if not candidates or total <= 0.0:
                groups[group] = {
                    "status": "not_measured",
                    "reason": "没有因子同时满足出现率闸与非零区分度",
                    "proposed_weights": None,
                }
                continue
            groups[group] = {
                "status": "measured",
                "proposed_weights": {factor: round(item["discrimination"] / total, 6) for factor, item in sorted(candidates.items())},
                "discrimination": {factor: item["discrimination"] for factor, item in sorted(candidates.items())},
                "excluded_low_presence": [declaration["key"] for declaration in declarations if declaration["key"] not in candidates],
            }
        hazards[hazard] = {"status": "measured", "pairs": len(cases), "groups": groups}

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "method": METHOD,
        "method_note": (
            "组内候选权重 = 因子区分度（正负组归一化均值差的绝对值）归一。"
            "它是第一根透明标尺，不是最优解；候选权重必须经人工评审才能以新版本写进 FACTOR_CONFIG。"
        ),
        "min_pairs": MIN_PAIRS,
        "min_factor_presence": MIN_FACTOR_PRESENCE,
        "inputs": {
            "labels": {"file": str(labels_path) if labels_path else None, "note": label_note},
            "observations": {"file": str(observations_path) if observations_path else None, "rows": len(observations)},
        },
        "applied": False,
        "weight_set_version": f"calib-{stamp}-candidate" if any(h["status"] == "measured" for h in hazards.values()) else None,
        "hazards": hazards,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="因子权重校准骨架（真值区分度法；缺真值时如实 not_measured）")
    parser.add_argument("--labels", type=Path, default=None, help="warning_truth_labels 导出 JSONL（格式同 accuracy.read_label_file）")
    parser.add_argument("--observations", type=Path, default=None, help="逐案例因子观测 JSONL（assess.v1 证据包导出）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="报告输出目录")
    args = parser.parse_args(argv)

    report = calibrate(args.labels, args.observations)
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / f"weight_calibration_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    measured = [hazard for hazard, item in report["hazards"].items() if item["status"] == "measured"]
    print(f"报告：{out_path}")
    print(f"measured {len(measured)} 个灾种；其余 not_measured（原因在报告里）——applied=false，生效须人工评审")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
