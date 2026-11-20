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


def mock_boundary_source(valid_region: str = "西藏（未校准）") -> dict:
    """Mock 归一化边界的来源登记（每次调用返回新 dict，避免共享可变状态）。

    归一化边界（`normalize.L/U`）此前是**裸字面量**——它落在 `weight_source`（标了 mock）
    与 `spec_reference`（指向规范但规范里没有这个数）之间的缝里：既没标 mock，
    也没给条款号。于是"150 mm/h 到底哪来的"没人答得上，而它恰恰决定了雨量因子
    什么时候饱和。登记一条出来，比让读者猜好。
    """
    return {
        "source_reference": "项目原型自拟归一化边界（无专业来源）",
        "method": "mock",
        "valid_region": valid_region,
        "validation_status": "mock_assumption",
    }


def _clip01(x):
    return max(0.0, min(1.0, float(x)))


def _saturate_note(value: float, lo: float, hi: float) -> str:
    """越出归一化边界时的说明。

    此前越界**静默饱和**（note 是空串）：一个 330 mm/h 的读数在 150 的上界下拿满分因子，
    而证据包、因子分、覆盖率、告警里都看不出任何异常——它长得像一个"刚好 150 的正常值"。
    边界是方法的标定带，越界只说明方法在外推（不是读数坏了），所以**不拒**，
    但要说出来：谁看因子分都能看见"这一项是外推出来的"。
    """
    if value < lo:
        return f"取值 {value:g} 低于归一化下界 {lo:g}，已截到 0"
    if value > hi:
        return f"取值 {value:g} 高于归一化上界 {hi:g}，已饱和到 1"
    return ""


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
        return _clip01((x - lo) / (hi - lo)), _saturate_note(x, lo, hi)
    if method == "linear_neg":
        lo, hi = norm["L"], norm["U"]
        if hi <= lo:
            return None, "归一化边界配置错误 (U<=L)"
        return _clip01((hi - x) / (hi - lo)), _saturate_note(x, lo, hi)
    if method == "log_pos":
        lo, hi = norm["L"], norm["U"]
        if x <= 0:
            return None, "对数归一化要求取值>0"
        if lo <= 0 or hi <= lo:
            return None, "归一化边界配置错误"
        return _clip01((math.log10(x) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))), _saturate_note(x, lo, hi)
    if method == "sqrt_pos":
        u = norm["U"]
        if u <= 0:
            return None, "归一化边界配置错误"
        return _clip01(math.sqrt(max(0.0, x)) / math.sqrt(u)), _saturate_note(x, 0.0, u)
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


def _representative(item: dict, agg: str):
    """一条因子观测的代表值：类别因子取 `class_value`，数值因子按配置的聚合口径取。

    窗口聚合已经发生在平台证据构建层（`min/max/mean` 都随观测一起送来）；
    这里只选"这个因子在本次窗口里的代表值"：触发类（agg=max）取窗口最大值，
    孕灾类（agg=mean）取窗口均值，站点画像只有单值——`max_value` 就是它。
    """
    if item.get("class_value") is not None:
        return item["class_value"]
    preferred = ("mean_value", "max_value") if agg == "mean" else ("max_value", "mean_value")
    for name in preferred:
        value = item.get(name)
        if value is not None:
            return value
    return item.get("min_value")


def compute_factor_assessment(observations, region: str, config: dict) -> dict:
    """按灾种因子配置计算 P/T 分组得分、R 指数、规则命中与覆盖率。

    参数:
        observations: 平台证据包里的 `FactorObservation` dump 列表（按 `factor` 取用）
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
    rows = observations if isinstance(observations, list) else []
    by_factor: dict[str, dict] = {}
    for item in rows:
        if isinstance(item, dict) and isinstance(item.get("factor"), str) and item["factor"] not in by_factor:
            by_factor[item["factor"]] = item
    groups_cfg = config.get("groups", {})
    route_cfg = config.get("route") or None
    min_coverage = config.get("min_coverage", DEFAULT_MIN_COVERAGE)

    warnings = []

    # ---------- 路由判定（如崩塌破坏方式 / 泥石流水源类型）----------
    route_value, route_note = None, ""
    weights_override = {}
    if route_cfg:
        route_key = route_cfg["factor_key"]
        route_obs = by_factor.get(route_key)
        if route_obs is not None:
            route_value = _representative(route_obs, "class")
        variants = route_cfg.get("variants", {})
        if route_value in variants:
            weights_override = variants[route_value]
            # 变体引用了未在组里声明的因子 → 配置错误，响亮失败：评分循环只遍历已声明
            # 因子，静默吞掉等于"变体永远不生效 + route_note 谎报已启用"（泥石流水源型
            # 路由踩过这个坑，见 debris_flow_risk_tool T 组的声明注释）。
            undeclared = [
                f"{group}.{key}"
                for group, overrides in weights_override.items()
                for key in overrides
                if key not in {f["key"] for f in groups_cfg.get(group, [])}
            ]
            if undeclared:
                raise ValueError(
                    f"权重变体 '{route_value}' 引用了未声明的因子: {undeclared}"
                    "（评分只遍历组里声明的因子——请把变体因子补进对应组的声明，权重默认 0）"
                )
            route_note = f"路由取值 '{route_value}'，启用对应权重变体"
        else:
            route_note = f"路由因子 '{route_key}' 缺失或取值'{route_value}'无对应变体，使用默认权重"
            warnings.append(route_note)

    # ---------- 逐因子取观测值并归一化 ----------
    factor_scores = {}
    for group_name, factors in groups_cfg.items():
        for f in factors:
            key = f["key"]
            item = by_factor.get(key)
            agg = f.get("agg", "max")
            value = _representative(item, agg) if item is not None else None
            z, note = (None, "无观测值") if value is None else normalize_value(value, f["normalize"])
            if value is not None and z is None and not note:
                note = "取值无法映射"

            factor_scores[key] = {
                "label": f["label"],
                "group": group_name,
                "unit": f.get("unit", ""),
                "monitor": f.get("monitor", ""),
                "agg": agg,
                "raw_value": value,
                "samples": int(item.get("samples", 0)) if item is not None else 0,
                "observations": 1 if item is not None else 0,
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
    # 默认权重按组预计算：此前每个因子都 `next()` 线性扫一遍组内因子（O(F²)，
    # 组内 2–6 个因子时很小，但没有任何理由留着）。
    default_weights: dict[str, dict[str, float]] = {
        group: {f["key"]: f["weight"] for f in factors} for group, factors in groups_cfg.items()
    }

    def group_weight(group, key):
        override = weights_override.get(group)
        if override is not None:
            # 变体是**整组替换**语义：变体给的是该组路由后的完整权重图（作者按 Σ=1 归一，
            # 如 rockfall 三型、debris_flow 四型）。此前按 key 合并、组内其它因子保留默认
            # 权重——outburst 路由下"不下雨"的覆盖分母里还躺着 0.8 的降雨权重，
            # 0.6 的覆盖率门控会把只有联动标志的评估整单拒绝。
            if key in override:
                return override[key], "route_variant"
            return 0.0, "route_variant_zero"
        weight = default_weights.get(group, {}).get(key)
        return (weight, "default") if weight is not None else (None, "default")

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

    # 覆盖率门控：只对**进 R 公式的组**（P/T）当硬门。I 组是"单独输出、不并入 P/T"
    # 的佐证面（见本文件 docstring 与 glacial_lake_risk_tool 的组注释）——此前它也被
    # 当硬门，站点画像缺 downstream_distance_km 就把一份完整观测的冰湖评估整单拒绝
    # （insufficient_data、R=None），而平台观测本就未必带下游距离。I 缺数据只降
    # 置信度（confidence 只取 P/T 覆盖率），并写 warning 留痕。
    insufficient_reasons = []
    if missing_required:
        insufficient_reasons.append(f"必需因子缺失: {missing_required}")
    gate_groups = tuple(g for g in groups_cfg if g != "I")
    for group_name in gate_groups:
        cov = completeness.get(group_name, 0.0)
        if cov < min_coverage:
            insufficient_reasons.append(f"{group_name} 组数据覆盖率 {cov:.0%} 低于门控 {min_coverage:.0%}")
    if "I" in groups_cfg and completeness.get("I", 0.0) < min_coverage:
        warnings.append(f"I 组数据覆盖率 {completeness.get('I', 0.0):.0%} 低于门控 {min_coverage:.0%}（佐证面，不作硬门）")
    insufficient = bool(insufficient_reasons)

    # ---------- P / T / R ----------
    P = group_scores.get("P")
    T = group_scores.get("T")
    R = None
    if not insufficient and P is not None and T is not None:
        R = round(100.0 * P * T, 2)

    # 临时分级（MOCK 阈值；非官方等级）
    # R==0 是"算出来没有风险"，不是"算不出来"——写成 none；只有 R is None（数据不足）才是 unknown。
    provisional_level = "unknown"
    if R is not None:
        if PROVISIONAL_LEVEL_THRESHOLDS["high"] <= R:
            provisional_level = "high"
        elif PROVISIONAL_LEVEL_THRESHOLDS["medium"] <= R:
            provisional_level = "medium"
        elif R > 0:
            provisional_level = "low"
        else:
            provisional_level = "none"

    # ---------- 保护规则命中（独立于指数，不被综合评分稀释） ----------
    rule_hits = []
    for rule in config.get("protected_rules", []):
        when = rule.get("when", {})
        field, equals = when.get("field"), when.get("equals")
        hit_obs = by_factor.get(field)
        observed = None if hit_obs is None else _representative(hit_obs, "class")
        hit = observed is not None and str(observed) == str(equals)
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
        "coverage_basis": "weighted_factor_coverage",
        "min_coverage": min_coverage,
        "missing_factors": [k for k, v in factor_scores.items() if v["missing"]],
        "rule_hits": rule_hits,
        "route_value": route_value,
        "route_note": route_note,
        "warnings": warnings,
        "sample_count": sum(int(item.get("samples", 0)) for item in rows if isinstance(item, dict)),
        "rule_version": config.get("rule_version", "MOCK-v0.1"),
        "disclaimer": config.get("disclaimer", ""),
        "aggregation_note": (
            "窗口聚合在证据构建层完成；因子按声明的 agg 取代表值（触发类取窗口最大值、"
            "孕灾类取窗口均值，站点画像取单值）。分类因子取观测到的类别值。"
        ),
    }
