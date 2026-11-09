"""
因子引擎（灾种无关的风险计算框架）

【本文件角色】
  实现"孕灾条件 P / 动态触发 T / 影响条件 I"三组因子的通用计算：
  归一化 → 加权评分（缺失重归一）→ 组合指数 → 保护规则命中 → 数据覆盖门控。
  各灾种的因子清单、权重、归一化边界与保护规则由各自的
  * _risk_tool.py 因子配置声明，本文件不含任何灾种专属逻辑。

【★ R 公式的身份声明（2026-10-18 用户更正，必须原样引用）★】
    R_h(t) = 100 × P_h × T_h(t)
  这是【本项目为程序原型提出的候选组合公式】：
    - 不是 DZ/T 0438—2023 的规范条款；
    - 不是课题任务书明确规定的公式；
    - 未经本地历史灾害数据验证，不能据此声称具有真实预警能力。
  P_h 为归一化孕灾条件评分，T_h(t) 为动态触发评分，均在 0~1 之间。

【数据门控】
  - required 因子缺失（全部记录无值）→ insufficient_data，不输出 R；
  - 组内覆盖率（已获得数据的权重占比）低于 min_coverage → insufficient_data；
  - 非必需因子缺失【不填 0】，按剩余权重重归一并输出覆盖率与缺失清单。

【对外契约】
  compute_factor_assessment(events, region, config) -> 统一评估信封 dict
  提供给：五个 *_risk_tool.py（传入各自 FACTOR_CONFIG）→ risk_assess_node.py
"""

import math

FACTOR_ENGINE_VERSION = "FACTOR-ENGINE-v0.1"

R_FORMULA_EXPRESSION = "R_h(t) = 100 × P_h × T_h(t)"
R_FORMULA_STATUS = "prototype_candidate"
R_FORMULA_NOTE = (
    "本项目为程序原型自拟的候选组合公式：P_h 为归一化孕灾条件评分、T_h(t) 为动态触发评分"
    "（均 0~1）。该公式不是 DZ/T 0438—2023 的规范条款，也不是课题任务书规定的公式；"
    "未经本地历史灾害数据验证，不能据此声称具有真实预警能力。"
)

# 临时风险分级阈值（MOCK，仅供流程测试；正式分级阈值未确认，
# 不输出红/橙/黄/蓝等官方等级）
PROVISIONAL_LEVEL_THRESHOLDS = {"high": 65.0, "medium": 40.0}
PROVISIONAL_LEVEL_STATUS = "mock_thresholds"

DEFAULT_MIN_COVERAGE = 0.6  # 组覆盖率门控（MOCK 约定，无规范来源）


def mock_weight_source(valid_region: str = "西藏（未校准）") -> dict:
    """Mock 测试权重的来源登记（每次调用返回新 dict，避免共享可变状态）。"""
    return {
        "source_reference": "项目原型自拟测试权重（无专业来源）",
        "method": "mock",
        "valid_region": valid_region,
        "validation_status": "mock_assumption",
    }


def literature_weight_source(reference: str = "藏东南文献（转引自《5类灾害触发条件.docx》）") -> dict:
    """文献候选参数的来源登记（未核实适用范围，不得当正式参数）。"""
    return {
        "source_reference": reference,
        "method": "literature_candidate",
        "valid_region": "藏东南（文献背景）",
        "validation_status": "pending_review",
    }


def _clip01(x):
    return max(0.0, min(1.0, float(x)))


# ============================================================
# 归一化（方法与边界均登记在因子声明的 normalize 字段）
# ============================================================


def normalize_value(value, norm: dict):
    """按因子声明的归一化方法计算 z ∈ [0,1]。

    支持 method：
      linear_pos / linear_neg: 线性正向/反向（L, U）
      log_pos: 对数正向（L, U，要求 x>0）
      sqrt_pos: 平根正向（U）
      band_pos: 带状最优（L, band_lo, band_hi, U；带内=1，带外线性衰减）
      class_map: 分类映射 {取值: z}
    返回 (z 或 None, note)；无法归一化时 z=None 并给出原因。
    """
    method = norm.get("method")
    if value is None:
        return None, "无观测值"

    if method == "class_map":
        mapping = norm.get("class_map", {})
        if value in mapping:
            return _clip01(mapping[value]), ""
        return None, f"取值'{value}'不在分类映射内"

    try:
        x = float(value)
    except (TypeError, ValueError):
        return None, "无法转换为数值"

    if method == "linear_pos":
        lo, hi = norm["L"], norm["U"]
        if hi <= lo:
            return None, "归一化边界配置错误 (U<=L)"
        return _clip01((x - lo) / (hi - lo)), ""
    if method == "linear_neg":
        lo, hi = norm["L"], norm["U"]
        if hi <= lo:
            return None, "归一化边界配置错误 (U<=L)"
        return _clip01((hi - x) / (hi - lo)), ""
    if method == "log_pos":
        lo, hi = norm["L"], norm["U"]
        if x <= 0:
            return None, "对数归一化要求取值>0"
        if lo <= 0 or hi <= lo:
            return None, "归一化边界配置错误"
        return _clip01((math.log10(x) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))), ""
    if method == "sqrt_pos":
        u = norm["U"]
        if u <= 0:
            return None, "归一化边界配置错误"
        return _clip01(math.sqrt(max(0.0, x)) / math.sqrt(u)), ""
    if method == "band_pos":
        lo, b_lo, b_hi, hi = norm["L"], norm["band_lo"], norm["band_hi"], norm["U"]
        if b_hi <= b_lo:
            return None, "归一化边界配置错误 (band)"
        if b_lo <= x <= b_hi:
            return 1.0, ""
        if x < b_lo:
            return _clip01((x - lo) / (b_lo - lo)) if b_lo > lo else 0.0, ""
        return _clip01((hi - x) / (hi - b_hi)) if hi > b_hi else 0.0, ""

    return None, f"未知归一化方法 {method}"


# ============================================================
# 主计算
# ============================================================


def compute_factor_assessment(events, region: str, config: dict) -> dict:
    """按灾种因子配置计算 P/T 分组得分、R 指数、规则命中与覆盖率。

    参数:
        events: Data Agent 的观测记录列表
        region: 区域（透传）
        config: 灾种因子配置（来自各 *_risk_tool.py 的 FACTOR_CONFIG）：
          {
            "hazard_type", "hazard_type_cn",
            "groups": {"P": [因子声明], "T": [...], "I": [...]},   # I 组可选
            "route": {"factor_key": str, "variants": {取值: {"T": {因子: 权重}}}} 或 None,
            "protected_rules": [{"rule_id","description","when":{field,equals},
                                  "level_suggest","source_reference","validation_status"}],
            "required_factors": [因子键],
            "min_coverage": float (MOCK 默认 0.6),
            "rule_version", "disclaimer",
          }
        因子声明: {"key","label","group","unit","agg"(max/mean/route),
                   "normalize":{...}, "weight", "weight_source":{source_reference,
                   method, valid_region, validation_status},
                   "data_status"(mock/literature/pending/real), "spec_reference",
                   "monitor"(监测方式), "required"(bool)}

    返回: 统一评估信封（见文件头与测试）。
    """
    records = events if isinstance(events, list) else []
    groups_cfg = config.get("groups", {})
    route_cfg = config.get("route") or None
    min_coverage = config.get("min_coverage", DEFAULT_MIN_COVERAGE)

    warnings = []

    # ---------- 路由判定（如崩塌破坏方式 / 泥石流水源类型）----------
    route_value, route_note = None, ""
    weights_override = {}
    if route_cfg:
        route_key = route_cfg["factor_key"]
        for rec in records:
            if isinstance(rec, dict) and rec.get(route_key) is not None:
                route_value = rec[route_key]
                break
        variants = route_cfg.get("variants", {})
        if route_value in variants:
            weights_override = variants[route_value]
            route_note = f"路由取值 '{route_value}'，启用对应权重变体"
        else:
            route_note = f"路由因子 '{route_key}' 缺失或取值'{route_value}'无对应变体，使用默认权重"
            warnings.append(route_note)

    # ---------- 逐因子聚合与归一化 ----------
    factor_scores = {}
    for group_name, factors in groups_cfg.items():
        for f in factors:
            key = f["key"]
            values = [rec.get(f["source_field"]) for rec in records if isinstance(rec, dict) and rec.get(f["source_field"]) is not None]
            coverage = (len(values) / len(records)) if records else 0.0

            z, note = None, ""
            if f.get("type", "numeric") == "class":
                # 分类因子：逐记录映射 z 后按 agg 聚合（max=触发性取最不利）
                zs = [normalize_value(v, f["normalize"])[0] for v in values]
                zs = [x for x in zs if x is not None]
                if zs:
                    z = max(zs) if f.get("agg", "max") == "max" else sum(zs) / len(zs)
                elif values:
                    note = "取值无法映射"
            else:
                if values:
                    agg = f.get("agg", "max")
                    agg_value = max(values) if agg == "max" else sum(values) / len(values)
                    z, note = normalize_value(agg_value, f["normalize"])
                else:
                    note = "无观测值"

            factor_scores[key] = {
                "label": f["label"],
                "group": group_name,
                "unit": f.get("unit", ""),
                "monitor": f.get("monitor", ""),
                "agg": f.get("agg", "max"),
                "raw_value": (
                    max(values) if (values and f.get("agg", "max") == "max") else (round(sum(values) / len(values), 3) if values else None)
                ),
                "record_coverage": round(coverage, 3),
                "z": z,
                "weight": None,  # 路由确定后填充
                "weight_source": f.get("weight_source", {}),
                "normalize": f["normalize"],
                "data_status": f.get("data_status", "mock"),
                "spec_reference": f.get("spec_reference", ""),
                "missing": z is None,
                "note": note,
            }

    # ---------- 权重确定（含路由变体） ----------
    def group_weight(group, key):
        if key in weights_override.get(group, {}):
            return weights_override[group][key], "route_variant"
        factor = next((f for f in groups_cfg.get(group, []) if f["key"] == key), None)
        return (factor["weight"], "default") if factor else (None, "default")

    # ---------- 分组评分（缺失重归一 + 覆盖率门控） ----------
    group_scores, completeness, missing_required = {}, {}, []
    for group_name, factors in groups_cfg.items():
        total_w, covered_w, weighted_sum = 0.0, 0.0, 0.0
        for f in factors:
            key = f["key"]
            w, w_kind = group_weight(group_name, key)
            factor_scores[key]["weight"] = w
            factor_scores[key]["weight_kind"] = w_kind
            total_w += w
            if factor_scores[key]["z"] is not None:
                covered_w += w
                weighted_sum += w * factor_scores[key]["z"]
        completeness[group_name] = round(covered_w / total_w, 3) if total_w > 0 else 0.0
        group_scores[group_name] = round(weighted_sum / covered_w, 4) if covered_w > 0 else None

    # required 因子检查
    for key in config.get("required_factors", []):
        if factor_scores.get(key, {}).get("z") is None:
            missing_required.append(key)
    if missing_required:
        warnings.append(f"必需因子缺失: {missing_required}")

    # 覆盖率门控
    insufficient_reasons = []
    if missing_required:
        insufficient_reasons.append(f"必需因子缺失: {missing_required}")
    for group_name in groups_cfg:
        cov = completeness.get(group_name, 0.0)
        if cov < min_coverage:
            insufficient_reasons.append(f"{group_name} 组数据覆盖率 {cov:.0%} 低于门控 {min_coverage:.0%}")
    insufficient = bool(insufficient_reasons)

    # ---------- P / T / R ----------
    P = group_scores.get("P")
    T = group_scores.get("T")
    R = None
    if not insufficient and P is not None and T is not None:
        R = round(100.0 * P * T, 2)

    # 临时分级（MOCK 阈值；非官方等级）
    provisional_level = "unknown"
    if R is not None:
        if PROVISIONAL_LEVEL_THRESHOLDS["high"] <= R:
            provisional_level = "high"
        elif PROVISIONAL_LEVEL_THRESHOLDS["medium"] <= R:
            provisional_level = "medium"
        elif R > 0:
            provisional_level = "low"

    # ---------- 保护规则命中（独立于指数，不被综合评分稀释） ----------
    rule_hits = []
    for rule in config.get("protected_rules", []):
        when = rule.get("when", {})
        field, equals = when.get("field"), when.get("equals")
        hit = any(isinstance(rec, dict) and rec.get(field) == equals for rec in records)
        if hit:
            rule_hits.append(
                {
                    "rule_id": rule["rule_id"],
                    "description": rule["description"],
                    "evidence_field": field,
                    "evidence_value": equals,
                    "level_suggest": rule.get("level_suggest", ""),
                    "source_reference": rule.get("source_reference", ""),
                    "validation_status": rule.get("validation_status", "mock"),
                }
            )

    # ---------- 组装信封 ----------
    return {
        "hazard_type": config.get("hazard_type", ""),
        "hazard_type_cn": config.get("hazard_type_cn", ""),
        "region": region,
        "engine_version": FACTOR_ENGINE_VERSION,
        "assessment_status": "insufficient_data" if insufficient else "scored",
        "required_factors": list(config.get("required_factors", [])),
        "insufficient_reasons": insufficient_reasons,
        "formula": {
            "expression": R_FORMULA_EXPRESSION,
            "status": R_FORMULA_STATUS,
            "note": R_FORMULA_NOTE,
        },
        "P": P,
        "T": T,
        "R": R,
        "provisional_level": provisional_level,
        "provisional_level_status": PROVISIONAL_LEVEL_STATUS,
        "factor_scores": factor_scores,
        "completeness": completeness,
        "min_coverage": min_coverage,
        "missing_factors": [k for k, v in factor_scores.items() if v["missing"]],
        "rule_hits": rule_hits,
        "route_value": route_value,
        "route_note": route_note,
        "warnings": warnings,
        "record_count": len(records),
        "rule_version": config.get("rule_version", "MOCK-v0.1"),
        "disclaimer": config.get("disclaimer", ""),
        "aggregation_note": ("触发类因子取记录最大值、孕灾类因子取均值（原型聚合约定，无规范来源）；分类因子按最不利取值。"),
    }
