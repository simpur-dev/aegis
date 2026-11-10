"""
Feedback Agent 确定性检查工具（全项目结构化质量检查引擎）

【本文件角色】
  用纯 Python（不调用 LLM）对 Data / Risk / Simulation / Warning 的最终交付物
  做完整性、一致性、版本、时间、状态与 Mock 声明检查，产出结构化问题清单。
  LLM 只在后续 Reflection / Suggestion 节点做解释与归因，
  【一切"问题是否存在"的判定都以本工具的确定性证据为准】。

【八类问题分类】（对齐任务书）
  data_quality_issue / risk_calculation_issue / simulation_issue /
  warning_consistency_issue / hazard_type_mismatch / model_version_issue /
  insufficient_evidence / workflow_error

【诚实声明 / 重要红线】
  1. 本工具只检查【结构与声明一致性】，不重新计算风险、不评判专业模型效力；
  2. 不认定"专业模型计算错误"——只报告可由证据核验的不一致/缺失；
  3. Mock 数据时间戳固定为历史值属于已知约定（见 data_validate_node 注释），
     processed_data.is_mock=True 时跳过过期检查，避免虚假问题；
  4. 检查规则全部从既有灾种注册表读取，不在本文件另造权重/阈值。

【对外契约】
  run_deterministic_checks(event_id, hazard_type, data_result, risk_result,
                           simulation_result, warning_result, external_feedback,
                           data_max_age_hours) -> list[issue]
  issue = {issue_id, category, agent, severity, fields, description,
           evidence, confidence, requires_human_review}
"""

import re
from datetime import UTC, datetime

from aegis.agents.hazards.hazard_registry import (
    ICE_ROCK_SNOW_AVALANCHE,
    SUPPORTED_HAZARDS,
    normalize_hazard_type,
)

# ------------------------------------------------------------
# 常量口径（与各 Agent 输出 Schema 对齐）
# ------------------------------------------------------------

CATEGORY_DATA_QUALITY = "data_quality_issue"
CATEGORY_RISK_CALC = "risk_calculation_issue"
CATEGORY_SIMULATION = "simulation_issue"
CATEGORY_WARNING_CONSISTENCY = "warning_consistency_issue"
CATEGORY_HAZARD_MISMATCH = "hazard_type_mismatch"
CATEGORY_MODEL_VERSION = "model_version_issue"
CATEGORY_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
CATEGORY_WORKFLOW_ERROR = "workflow_error"

ALL_CATEGORIES = [
    CATEGORY_DATA_QUALITY,
    CATEGORY_RISK_CALC,
    CATEGORY_SIMULATION,
    CATEGORY_WARNING_CONSISTENCY,
    CATEGORY_HAZARD_MISMATCH,
    CATEGORY_MODEL_VERSION,
    CATEGORY_INSUFFICIENT_EVIDENCE,
    CATEGORY_WORKFLOW_ERROR,
]

# 各 Agent 状态合法枚举（与各 main/graph 输出约定一致）
VALID_STATUS = {
    "data": {"success", "partial", "failed", "processing", "pending", ""},
    "risk": {"success", "uncertain", "insufficient_data", "failed", ""},
    "simulation": {"success", "uncertain", "insufficient_data", "failed", ""},
    "warning": {"draft", "validation_failed", "insufficient_data", ""},
}
VALID_WARNING_LEVELS = {"none", "advisory", "watch", "warning", ""}

# "把 Mock 说成真实"的声明性关键词与否定词（句级扫描，否定词防误报：
# 例如"本结果非真实预测"不得被误判为冒充真实）
_REAL_CLAIM_KEYWORDS = [
    "真实观测数据",
    "真实数据来源",
    "真实数据支持",
    "真实预测结果",
    "已正式发布",
    "正式发布",
    "已发布预警",
    "非模拟数据",
    "并非模拟",
]
_NEGATION_MARKERS = [
    "不是",
    "并非",
    "非真实",
    "禁止",
    "不得",
    "未发布",
    "仅为",
    "只用于",
    "模拟",
    "Mock",
    "mock",
    "MOCK",
    "无真实",
    "不真实",
    "玩具",
]

_ISO_Z_RE = re.compile(r"Z$")


def _parse_time(value):
    """解析 ISO8601 时间字符串（兼容尾部 Z）；失败返回 None。"""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(_ISO_Z_RE.sub("+00:00", value.strip()))
    except Exception:
        return None


def _issue(seq, category, agent, severity, fields, description, evidence, confidence=0.9, requires_human_review=False):
    """issue 记录快捷构造。"""
    return {
        "issue_id": f"ISS-{seq:03d}",
        "category": category,
        "agent": agent,  # data / risk / simulation / warning / manager
        "severity": severity,  # info / minor / major / critical
        "fields": list(fields),
        "description": description,
        "evidence": [str(e) for e in evidence],
        "confidence": float(confidence),
        "requires_human_review": bool(requires_human_review),
    }


# ============================================================
# 主入口
# ============================================================


def run_deterministic_checks(
    event_id: str,
    hazard_type: str,
    data_result: dict,
    risk_result: dict,
    simulation_result: dict | None = None,
    warning_result: dict | None = None,
    external_feedback=None,
    data_max_age_hours: float = 72.0,
) -> list:
    """执行全部确定性检查，返回 issue 清单（不修改任何入参对象）。"""
    simulation_result = simulation_result or {}
    warning_result = warning_result or {}
    data_result = data_result or {}
    risk_result = risk_result or {}

    issues = []
    seq = _Counter()

    hazard_key = normalize_hazard_type(hazard_type)

    # ---------- 0. 灾种有效性（无效则拒绝后续全部检查） ----------
    if hazard_key not in SUPPORTED_HAZARDS:
        issues.append(
            _issue(
                seq(),
                CATEGORY_WORKFLOW_ERROR,
                "manager",
                "critical",
                ["hazard_type"],
                f"灾种'{hazard_type}'不在五类标准灾种内，Feedback Agent 拒绝检查"
                "（本项目仅支持 landslide/rockfall/debris_flow/avalanche/"
                "lake_outburst，不包含地震）",
                [f"输入 hazard_type={hazard_type!r}", f"支持清单={SUPPORTED_HAZARDS}"],
                confidence=0.99,
            )
        )
        return issues

    # ---------- 1. 必备上游结果存在性 ----------
    if not data_result:
        issues.append(
            _issue(
                seq(),
                CATEGORY_INSUFFICIENT_EVIDENCE,
                "data",
                "major",
                ["data_result"],
                "缺少 Data Agent 结果，无法核验数据质量与风险评估依据",
                ["data_result 为空"],
                confidence=0.95,
            )
        )
    if not risk_result:
        issues.append(
            _issue(
                seq(),
                CATEGORY_INSUFFICIENT_EVIDENCE,
                "risk",
                "major",
                ["risk_result"],
                "缺少 Risk Agent 结果，反馈证据不足（无法完成风险-推演-预警一致性链路检查）",
                ["risk_result 为空"],
                confidence=0.95,
            )
        )

    # ---------- 2. 灾种一致性（跨 Agent 逐块比对） ----------
    _check_hazard_consistency(seq, issues, hazard_key, data_result, risk_result, simulation_result, warning_result)

    # ---------- 3. 状态级联一致性 ----------
    _check_status_cascade(seq, issues, data_result, risk_result, simulation_result, warning_result)

    # ---------- 4. 各 Agent 分块检查 ----------
    if data_result:
        _check_data_block(seq, issues, hazard_key, data_result, data_max_age_hours)
    if risk_result:
        _check_risk_block(seq, issues, hazard_key, risk_result, data_result)
    if simulation_result:
        _check_simulation_block(seq, issues, hazard_key, simulation_result)
    if warning_result:
        _check_warning_block(seq, issues, hazard_key, warning_result, risk_result, data_result, simulation_result)

    # ---------- 5. 灾种差异化检查 ----------
    _check_hazard_specific(seq, issues, hazard_key, data_result, risk_result)

    # ---------- 6. 外部反馈接口（格式核验，不做真伪判断） ----------
    if external_feedback:
        from aegis.agents.hazards.feedback_agent.tools.feedback_memory_tool import (
            validate_external_feedback,
        )

        records = external_feedback if isinstance(external_feedback, list) else [external_feedback]
        for i, rec in enumerate(records):
            problems = validate_external_feedback(rec)
            for p in problems:
                issues.append(
                    _issue(
                        seq(),
                        CATEGORY_INSUFFICIENT_EVIDENCE,
                        "manager",
                        "minor",
                        [f"external_feedback[{i}]"],
                        f"外部反馈记录格式不完整：{p}",
                        [f"记录序号={i}", p],
                        confidence=0.9,
                        requires_human_review=True,
                    )
                )

    return issues


class _Counter:
    """issue 自增编号器。"""

    def __init__(self):
        self.n = 0

    def __call__(self):
        self.n += 1
        return self.n


# ============================================================
# 2. 灾种一致性
# ============================================================


def _check_hazard_consistency(seq, issues, hazard_key, data_result, risk_result, simulation_result, warning_result):
    """各结果块声明的 hazard_type（含 alert 内嵌灾种）必须与事件灾种一致。"""
    blocks = [
        ("data", data_result.get("hazard_type") or (data_result.get("processed_data", {}) or {}).get("hazard_type")),
        ("risk", risk_result.get("hazard_type")),
        ("simulation", simulation_result.get("hazard_type")),
        ("warning", warning_result.get("hazard_type")),
    ]
    for agent, declared in blocks:
        if declared in (None, ""):
            continue  # 缺失由分块检查报，不重复
        if normalize_hazard_type(str(declared)) != hazard_key:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_HAZARD_MISMATCH,
                    agent,
                    "critical",
                    [f"{agent}_result.hazard_type"],
                    f"{agent.upper()} Agent 结果的灾种标识'{declared}'与事件灾种'{hazard_key}'不一致（疑似路由错误灾种的工具/模型）",
                    [f"事件灾种={hazard_key}", f"{agent} 声明灾种={declared}"],
                    confidence=0.95,
                )
            )

    # Warning 的 alert 对象内嵌灾种单独核对
    alert = (warning_result.get("alert") or {}) if warning_result else {}
    if alert.get("hazard_type") and normalize_hazard_type(alert["hazard_type"]) != hazard_key:
        issues.append(
            _issue(
                seq(),
                CATEGORY_HAZARD_MISMATCH,
                "warning",
                "critical",
                ["warning_result.alert.hazard_type"],
                f"预警对象灾种'{alert['hazard_type']}'与事件灾种'{hazard_key}'不一致",
                [f"alert.hazard_type={alert['hazard_type']}", f"事件灾种={hazard_key}"],
                confidence=0.95,
            )
        )


# ============================================================
# 3. 状态级联
# ============================================================


def _check_status_cascade(seq, issues, data_result, risk_result, simulation_result, warning_result):
    """上游失败/数据不足时下游却'成功'，属于流程性矛盾（workflow_error）。"""
    d_status = data_result.get("status", "")
    r_status = risk_result.get("status", "")
    s_status = simulation_result.get("status", "")
    w_status = warning_result.get("status", "")

    if d_status == "failed" and r_status == "success":
        issues.append(
            _issue(
                seq(),
                CATEGORY_WORKFLOW_ERROR,
                "manager",
                "major",
                ["data_result.status", "risk_result.status"],
                "Data Agent 状态为 failed 但 Risk Agent 报告 success，状态级联矛盾",
                [f"data.status={d_status}", f"risk.status={r_status}"],
                confidence=0.85,
            )
        )
    if r_status in ("insufficient_data", "failed") and s_status == "success":
        issues.append(
            _issue(
                seq(),
                CATEGORY_WORKFLOW_ERROR,
                "manager",
                "major",
                ["risk_result.status", "simulation_result.status"],
                f"Risk Agent 状态为 {r_status} 但 Simulation Agent 报告 success，状态级联矛盾",
                [f"risk.status={r_status}", f"sim.status={s_status}"],
                confidence=0.85,
            )
        )
    if r_status in ("insufficient_data", "failed") and w_status == "draft":
        issues.append(
            _issue(
                seq(),
                CATEGORY_WORKFLOW_ERROR,
                "manager",
                "major",
                ["risk_result.status", "warning_result.status"],
                f"Risk Agent 状态为 {r_status} 但 Warning Agent 仍生成草稿(draft)，状态级联矛盾",
                [f"risk.status={r_status}", f"warning.status={w_status}"],
                confidence=0.85,
            )
        )

    for agent, status in (("data", d_status), ("risk", r_status), ("simulation", s_status), ("warning", w_status)):
        if status and status not in VALID_STATUS[agent]:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_WORKFLOW_ERROR,
                    agent,
                    "major",
                    [f"{agent}_result.status"],
                    f"{agent.upper()} Agent 状态值'{status}'不在系统约定枚举内",
                    [f"status={status}", f"约定={sorted(VALID_STATUS[agent])}"],
                    confidence=0.9,
                )
            )


# ============================================================
# 4a. Data 块检查
# ============================================================


def _check_data_block(seq, issues, hazard_key, data_result, max_age_hours):
    processed = data_result.get("processed_data") or {}
    events = processed.get("events") or []

    # 必备结构字段
    for field in ("processed_data", "data_quality", "status"):
        if not data_result.get(field):
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_DATA_QUALITY,
                    "data",
                    "major" if field == "processed_data" else "minor",
                    [f"data_result.{field}"],
                    f"Data Agent 输出缺少必要字段 '{field}'",
                    [f"data_result 键={sorted(data_result.keys())}"],
                    confidence=0.9,
                )
            )

    quality = data_result.get("data_quality") or {}
    if quality and quality.get("is_valid") is False:
        issues.append(
            _issue(
                seq(),
                CATEGORY_DATA_QUALITY,
                "data",
                "major",
                ["data_result.data_quality.is_valid"],
                "数据质量检查未通过：" + str(quality.get("validation_reason") or quality.get("reason") or "未给出原因"),
                [f"is_valid={quality.get('is_valid')}", f"missing_fields_summary={quality.get('missing_fields_summary', {})}"],
                confidence=0.9,
            )
        )

    sufficiency = data_result.get("data_sufficiency") or {}
    if data_result.get("status") == "success" and sufficiency.get("sufficient") is False:
        issues.append(
            _issue(
                seq(),
                CATEGORY_DATA_QUALITY,
                "data",
                "major",
                ["data_result.data_sufficiency"],
                "Data 状态为 success 但数据充分性自评不足，存在'带病下游传'风险",
                [f"reason={sufficiency.get('reason')}"],
                confidence=0.85,
            )
        )

    if processed and not events:
        issues.append(
            _issue(
                seq(),
                CATEGORY_DATA_QUALITY,
                "data",
                "critical",
                ["data_result.processed_data.events"],
                "processed_data 存在但 events 记录为空，下游风险评估无原始依据",
                ["events 为空列表"],
                confidence=0.95,
            )
        )

    # 必需观测字段齐备性（字段清单来自 hazard_data_registry，不另造）
    if events:
        from aegis.agents.hazards.data_agent.tools.hazard_data_registry import get_hazard_data_requirements

        req = get_hazard_data_requirements(hazard_key)
        required = req["required_fields"]  # 公共必需 + 灾种必需观测字段
        for field in required:
            if not any(e.get(field) is not None for e in events):
                issues.append(
                    _issue(
                        seq(),
                        CATEGORY_DATA_QUALITY,
                        "data",
                        "major",
                        [f"processed_data.events[*].{field}"],
                        f"全部观测记录均缺失必需字段 '{field}'（{req['observation_fields'].get(field, {}).get('desc', '灾种必需观测项')}）",
                        [f"灾种={hazard_key}", f"required_fields={required}"],
                        confidence=0.9,
                    )
                )

        # 数据过期检查：Mock 数据时间戳固定为历史值属已知约定，跳过；
        # 非 Mock（接入真实数据源）时按 max_age_hours 核验
        is_mock = processed.get("is_mock", processed.get("data_source") == "mock")
        if not is_mock:
            now = datetime.now(UTC)
            fresh_times = [t for t in (_parse_time(e.get("time")) for e in events) if t]
            if fresh_times:
                newest = max(fresh_times)
                age_h = (now - newest).total_seconds() / 3600.0
                if age_h > max_age_hours:
                    issues.append(
                        _issue(
                            seq(),
                            CATEGORY_DATA_QUALITY,
                            "data",
                            "major",
                            ["processed_data.events[*].time"],
                            f"最新观测数据距今约 {age_h:.0f} 小时，超过时效阈值 {max_age_hours:.0f} 小时（数据过期）",
                            [f"最新时间={newest.isoformat()}", f"is_mock={is_mock}", f"阈值={max_age_hours}"],
                            confidence=0.85,
                        )
                    )
            else:
                issues.append(
                    _issue(
                        seq(),
                        CATEGORY_DATA_QUALITY,
                        "data",
                        "minor",
                        ["processed_data.events[*].time"],
                        "非 Mock 数据但时间字段全部无法解析，无法核验时效",
                        ["events[*].time 解析失败"],
                        confidence=0.6,
                        requires_human_review=True,
                    )
                )


# ============================================================
# 4b. Risk 块检查
# ============================================================


def _check_risk_block(seq, issues, hazard_key, risk_result, data_result):
    assessment = risk_result.get("assessment") or {}
    model_info = risk_result.get("model_info") or {}
    status = risk_result.get("status", "")

    for field in ("risk_level", "assessment", "model_info"):
        if not risk_result.get(field):
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_RISK_CALC,
                    "risk",
                    "minor",
                    [f"risk_result.{field}"],
                    f"Risk 结果缺少字段 '{field}'",
                    [f"risk_result 键={sorted(risk_result.keys())}"],
                    confidence=0.85,
                )
            )

    # 风险结果必须有计算依据：success 却无 R 值 / 无因子得分 → 依据缺失
    if status == "success":
        if assessment.get("R") is None:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_RISK_CALC,
                    "risk",
                    "major",
                    ["risk_result.assessment.R"],
                    "Risk 状态为 success 但组合指数 R 缺失，风险结论缺少计算依据",
                    [f"assessment_status={assessment.get('assessment_status')}", f"R={assessment.get('R')}"],
                    confidence=0.9,
                )
            )
        if not assessment.get("factor_scores"):
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_RISK_CALC,
                    "risk",
                    "major",
                    ["risk_result.assessment.factor_scores"],
                    "Risk 状态为 success 但因子得分为空，无法追溯计算过程",
                    ["factor_scores 为空"],
                    confidence=0.9,
                )
            )
        if not (risk_result.get("evidence") or []):
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_RISK_CALC,
                    "risk",
                    "minor",
                    ["risk_result.evidence"],
                    "Risk 状态为 success 但证据链为空（是否确有其据需人工复核，不据此认定计算错误）",
                    ["evidence 为空列表"],
                    confidence=0.5,
                    requires_human_review=True,
                )
            )

    # 自评矛盾：validation 未通过但状态 success
    validation = risk_result.get("validation") or {}
    if status == "success" and validation.get("is_valid") is False:
        issues.append(
            _issue(
                seq(),
                CATEGORY_RISK_CALC,
                "risk",
                "major",
                ["risk_result.validation"],
                "Risk 内部验证未通过但状态标记为 success，自评矛盾",
                ["is_valid=False", f"issues={validation.get('issues', [])}"],
                confidence=0.85,
            )
        )

    # 规则版本与灾种注册表匹配（版本不符 → 疑似用错灾种工具或旧规则）
    try:
        from aegis.agents.hazards.risk_agent.tools.hazard_risk_registry import get_hazard_risk_profile

        profile = get_hazard_risk_profile(hazard_key)
        declared_rule = model_info.get("rule_version", "")
        if declared_rule and declared_rule != profile.get("rule_version"):
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_MODEL_VERSION,
                    "risk",
                    "major",
                    ["risk_result.model_info.rule_version"],
                    f"Risk 结果 rule_version'{declared_rule}'与灾种注册表'{profile.get('rule_version')}'不匹配",
                    [f"registry={profile.get('rule_version')}", f"declared={declared_rule}"],
                    confidence=0.9,
                )
            )
        # 因子口径核对：必需因子是否出现在 factor_scores
        factor_scores = assessment.get("factor_scores") or {}
        if status == "success" and factor_scores:
            missing_f = [f for f in profile.get("required_input_fields", []) if f not in factor_scores]
            if missing_f:
                issues.append(
                    _issue(
                        seq(),
                        CATEGORY_RISK_CALC,
                        "risk",
                        "major",
                        ["risk_result.assessment.factor_scores"],
                        f"Risk 因子得分未覆盖灾种注册表必需因子 {missing_f}（疑似未使用正确灾种的计算工具）",
                        [f"必需因子={profile.get('required_input_fields')}", f"实际因子键={sorted(factor_scores.keys())}"],
                        confidence=0.85,
                    )
                )
    except Exception as e:
        issues.append(
            _issue(
                seq(),
                CATEGORY_WORKFLOW_ERROR,
                "risk",
                "info",
                ["risk_result"],
                f"Risk 版本/因子注册表核验跳过（注册表不可用）：{e}",
                [str(e)],
                confidence=0.7,
            )
        )

    # Mock 数据被标记为非 Mock（冒充真实）
    data_is_mock = (data_result.get("processed_data") or {}).get("is_mock")
    if data_is_mock is True and model_info.get("is_mock") is False:
        issues.append(
            _issue(
                seq(),
                CATEGORY_WORKFLOW_ERROR,
                "risk",
                "critical",
                ["risk_result.model_info.is_mock"],
                "上游数据为 Mock，但 Risk 结果声称非 Mock 实现（is_mock=False），涉嫌将模拟结果描述为真实计算",
                [f"data.is_mock={data_is_mock}", f"risk.model_info.is_mock={model_info.get('is_mock')}"],
                confidence=0.9,
            )
        )


# ============================================================
# 4c. Simulation 块检查
# ============================================================


def _check_simulation_block(seq, issues, hazard_key, simulation_result):
    model_info = simulation_result.get("model_info") or {}
    status = simulation_result.get("status", "")
    results = simulation_result.get("simulation_results") or []

    for field in ("model_info", "validation", "metadata"):
        if not simulation_result.get(field):
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_SIMULATION,
                    "simulation",
                    "minor",
                    [f"simulation_result.{field}"],
                    f"Simulation 结果缺少字段 '{field}'",
                    [f"键={sorted(simulation_result.keys())}"],
                    confidence=0.85,
                )
            )

    # 红线：Mock 模型不得声称真实预测
    if model_info.get("is_real_prediction") is True:
        issues.append(
            _issue(
                seq(),
                CATEGORY_WORKFLOW_ERROR,
                "simulation",
                "critical",
                ["simulation_result.model_info.is_real_prediction"],
                "Simulation 结果声称 is_real_prediction=True，违反系统红线（当前五灾种模型全部为 MOCK，禁止描述为真实预测）",
                ["model_info.is_real_prediction=True"],
                confidence=0.95,
            )
        )

    # 模型版本与模型路由注册表匹配
    try:
        from aegis.agents.hazards.simulation_agent.tools.hazard_model_router import get_model_requirements

        mreq = get_model_requirements(hazard_key)
        declared = model_info.get("model_version", "")
        if declared and declared != mreq["model_version"]:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_MODEL_VERSION,
                    "simulation",
                    "major",
                    ["simulation_result.model_info.model_version"],
                    f"Simulation 模型版本'{declared}'与灾种'{hazard_key}'注册模型版本'{mreq['model_version']}'不匹配（疑似路由错误模型）",
                    [f"registry={mreq['model_version']}", f"declared={declared}"],
                    confidence=0.9,
                )
            )
        # 逐情景结果的模型版本一致性
        for r in results:
            if r.get("model_version") and r["model_version"] != declared and declared:
                issues.append(
                    _issue(
                        seq(),
                        CATEGORY_MODEL_VERSION,
                        "simulation",
                        "minor",
                        [f"simulation_results[{r.get('scenario_id', '?')}].model_version"],
                        f"情景 {r.get('scenario_id')} 的模型版本'{r['model_version']}'与交付物声明'{declared}'不一致",
                        [f"scenario={r.get('scenario_id')}", f"entry={r['model_version']}", f"declared={declared}"],
                        confidence=0.85,
                    )
                )
    except Exception:
        pass

    # 状态与执行事实一致性
    routing = (simulation_result.get("validation") or {}).get("routing") or {}
    if status == "success" and not results:
        issues.append(
            _issue(
                seq(),
                CATEGORY_SIMULATION,
                "simulation",
                "major",
                ["simulation_result.simulation_results"],
                "Simulation 状态为 success 但无任何情景结果",
                ["simulation_results 为空"],
                confidence=0.9,
            )
        )
    if status == "success" and routing.get("inputs_ok") is False:
        issues.append(
            _issue(
                seq(),
                CATEGORY_SIMULATION,
                "simulation",
                "major",
                ["simulation_result.validation.routing.inputs_ok"],
                "路由判定模型输入不齐备(inputs_ok=False)但状态仍为 success，矛盾",
                [f"missing_inputs={routing.get('missing_inputs', [])}"],
                confidence=0.85,
            )
        )
    scenario_validation = (simulation_result.get("validation") or {}).get("scenario_validation") or {}
    if status == "success" and scenario_validation.get("recommendation") == "fail":
        issues.append(
            _issue(
                seq(),
                CATEGORY_SIMULATION,
                "simulation",
                "major",
                ["simulation_result.validation.scenario_validation"],
                "情景校验结论为 fail 但状态仍为 success，矛盾",
                [f"issues={scenario_validation.get('issues', [])}"],
                confidence=0.85,
            )
        )


# ============================================================
# 4d. Warning 块检查
# ============================================================


def _check_warning_block(seq, issues, hazard_key, warning_result, risk_result, data_result, simulation_result):
    alert = warning_result.get("alert") or {}
    status = warning_result.get("status", "")

    # draft 必须有预警对象
    if status == "draft" and not alert:
        issues.append(
            _issue(
                seq(),
                CATEGORY_WARNING_CONSISTENCY,
                "warning",
                "critical",
                ["warning_result.alert"],
                "Warning 状态为 draft 但预警对象缺失",
                ["alert 为空"],
                confidence=0.95,
            )
        )
        return

    if alert:
        # 与 Risk 交付物的一致性（依据链快照 vs 最新 Risk 结果）
        based_on = alert.get("based_on") or {}
        risk_level_now = risk_result.get("risk_level", "")
        if based_on.get("risk_level") and risk_level_now and based_on["risk_level"] != risk_level_now:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_WARNING_CONSISTENCY,
                    "warning",
                    "major",
                    ["warning_result.alert.based_on.risk_level"],
                    f"预警依据 risk_level='{based_on['risk_level']}'与 Risk 当前"
                    f"risk_level='{risk_level_now}'不一致（预警未基于最新风险结果或字段被改动）",
                    [f"based_on={based_on.get('risk_level')}", f"risk_now={risk_level_now}"],
                    confidence=0.85,
                )
            )
        r_now = (risk_result.get("assessment") or {}).get("R")
        if based_on.get("R") is not None and r_now is not None and based_on["R"] != r_now:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_WARNING_CONSISTENCY,
                    "warning",
                    "major",
                    ["warning_result.alert.based_on.R"],
                    f"预警依据 R={based_on.get('R')} 与 Risk 当前 R={r_now} 不一致",
                    [f"based_on.R={based_on.get('R')}", f"assessment.R={r_now}"],
                    confidence=0.85,
                )
            )

        # Mock 声明一致性
        data_is_mock = (data_result.get("processed_data") or {}).get("is_mock")
        if data_is_mock is True and alert.get("mock_only") is False:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_WARNING_CONSISTENCY,
                    "warning",
                    "critical",
                    ["warning_result.alert.mock_only"],
                    "上游数据为 Mock 但预警对象声称 mock_only=False（把模拟结果描述为真实预警），违反红线",
                    ["data.is_mock=True", f"alert.mock_only={alert.get('mock_only')}"],
                    confidence=0.9,
                )
            )

        # 预警等级枚举与映射版本
        if alert.get("warning_level", "") not in VALID_WARNING_LEVELS:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_WARNING_CONSISTENCY,
                    "warning",
                    "major",
                    ["warning_result.alert.warning_level"],
                    f"预警等级'{alert.get('warning_level')}'不在系统约定枚举内",
                    [f"warning_level={alert.get('warning_level')}"],
                    confidence=0.9,
                )
            )
        try:
            from aegis.agents.hazards.warning_agent.tools.hazard_warning_rule_registry import get_hazard_warning_rules

            registry_mapping = get_hazard_warning_rules(hazard_key)["mapping_version"]
            declared = alert.get("mapping_version", "")
            if declared and declared != registry_mapping:
                issues.append(
                    _issue(
                        seq(),
                        CATEGORY_MODEL_VERSION,
                        "warning",
                        "major",
                        ["warning_result.alert.mapping_version"],
                        f"预警等级映射版本'{declared}'与灾种注册表'{registry_mapping}'不匹配",
                        [f"registry={registry_mapping}", f"declared={declared}"],
                        confidence=0.9,
                    )
                )
        except Exception:
            pass

        # 区域一致性（预警目标区域 vs 观测区域）
        events = (data_result.get("processed_data") or {}).get("events") or []
        regions = {e.get("region") for e in events if e.get("region")}
        target = alert.get("target_region", "")
        if regions and target and target != "未标注区域" and not any(target in r or r in target for r in regions):
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_WARNING_CONSISTENCY,
                    "warning",
                    "minor",
                    ["warning_result.alert.target_region"],
                    f"预警目标区域'{target}'与观测记录区域{sorted(regions)}不一致",
                    [f"target_region={target}", f"data regions={sorted(regions)}"],
                    confidence=0.6,
                    requires_human_review=True,
                )
            )

        # 生命周期时间合理性
        vf, vu = _parse_time(alert.get("valid_from")), _parse_time(alert.get("valid_until"))
        if vf and vu and vu < vf:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_WARNING_CONSISTENCY,
                    "warning",
                    "major",
                    ["warning_result.alert.valid_until"],
                    "预警失效时间早于生效时间，时间窗矛盾",
                    [f"valid_from={alert.get('valid_from')}", f"valid_until={alert.get('valid_until')}"],
                    confidence=0.9,
                )
            )

    # 解释文本中的"真实/已发布"不当声明（句级扫描 + 否定词防误报）
    explanation = (warning_result.get("explanation") or {}).get("text", "") or ""
    for sent in re.split(r"[。；;\n]", explanation):
        sent = sent.strip()
        if not sent:
            continue
        if any(k in sent for k in _REAL_CLAIM_KEYWORDS) and not any(n in sent for n in _NEGATION_MARKERS):
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_WARNING_CONSISTENCY,
                    "warning",
                    "critical",
                    ["warning_result.explanation.text"],
                    "预警说明把模拟/草稿结果描述为真实观测或正式发布，与 Mock 标记矛盾（句级证据见 evidence）",
                    [f"可疑句子：{sent[:80]}"],
                    confidence=0.75,
                    requires_human_review=True,
                )
            )
        if "已发布" in sent and alert.get("alert_status") == "draft" and not any(n in sent for n in _NEGATION_MARKERS):
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_WARNING_CONSISTENCY,
                    "warning",
                    "major",
                    ["warning_result.explanation.text"],
                    "预警对象生命周期为 draft，但说明文本声称已发布",
                    [f"可疑句子：{sent[:80]}"],
                    confidence=0.75,
                    requires_human_review=True,
                )
            )


# ============================================================
# 5. 灾种差异化检查（字段清单读注册表，不另造阈值）
# ============================================================


def _check_hazard_specific(seq, issues, hazard_key, data_result, risk_result):
    from aegis.agents.hazards.feedback_agent.tools.hazard_feedback_rules import get_hazard_focus_checks

    focus = get_hazard_focus_checks(hazard_key)
    events = (data_result.get("processed_data") or {}).get("events") or []
    if not events:
        return

    # 5a. 关注字段完全未出现在观测数据中 → 该灾种触发路径未被处理
    for field in focus["key_fields"]:
        if not any(field in e for e in events):
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_DATA_QUALITY,
                    "data",
                    "minor",
                    [f"processed_data.events[*].{field}"],
                    f"【{hazard_key} 差异化检查】关注观测字段 '{field}'"
                    f"（{focus['field_desc'].get(field, '')}）未出现在任何观测记录中；"
                    f"灾种关注项：{focus['focus_note']}",
                    [f"重点字段={focus['key_fields']}"],
                    confidence=0.8,
                )
            )

    # 5b. 分类路由因子取值合法性（枚举来自注册表 allowed 集合）
    for field, allowed in focus["class_fields"].items():
        bad = [{e.get("event_id"): e.get(field)} for e in events if e.get(field) is not None and e.get(field) not in allowed]
        if bad:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_DATA_QUALITY,
                    "data",
                    "major",
                    [f"processed_data.events[*].{field}"],
                    f"【{hazard_key} 差异化检查】分类字段 '{field}' 出现注册表外取值（路由因子非法会导致破坏方式/触发路径匹配错误）",
                    [f"非法取值样例={bad[:3]}", f"允许枚举={allowed}"],
                    confidence=0.9,
                )
            )

    # 5c. 数值超出物理合理范围（范围来自注册表 valid_ranges，数据质量口径）
    for field, (lo, hi) in focus["valid_ranges"].items():
        out = [
            {e.get("event_id"): e.get(field)}
            for e in events
            if isinstance(e.get(field), (int, float)) and not isinstance(e.get(field), bool) and not (lo <= e[field] <= hi)
        ]
        if out:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_DATA_QUALITY,
                    "data",
                    "major",
                    [f"processed_data.events[*].{field}"],
                    f"【{hazard_key} 差异化检查】数值字段 '{field}' 超出物理合理范围 [{lo}, {hi}]（数据质量口径，非风险阈值）",
                    [f"越界样例={out[:3]}"],
                    confidence=0.9,
                )
            )

    # 5d. 雪崩类：观测值与模型适用范围的一致性（参数范围来自模型菜单）
    if hazard_key == ICE_ROCK_SNOW_AVALANCHE and focus["model_param_ranges"]:
        out_of_scope = []
        for field, (lo, hi) in focus["model_param_ranges"].items():
            for e in events:
                v = e.get(field)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and not (lo <= v <= hi):
                    out_of_scope.append({field: v, "event": e.get("event_id"), "model_range": [lo, hi]})
        if out_of_scope:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_SIMULATION,
                    "simulation",
                    "minor",
                    ["simulation scenario params vs model range"],
                    "【冰岩雪崩差异化检查】观测值超出 Mock 模型参数适用范围（积雪/坡度/气温应与模型适用范围一致）",
                    [f"样例={out_of_scope[:3]}"],
                    confidence=0.6,
                    requires_human_review=True,
                )
            )

    # 5e. Risk 因子得分对灾种重点因子的覆盖（缺失=处理不合理的线索，仅提示复核）
    factor_scores = ((risk_result.get("assessment") or {}).get("factor_scores")) or {}
    if risk_result.get("status") == "success" and factor_scores:
        uncovered = [f for f in focus["key_fields"] if f in events[0] and f not in factor_scores and f not in (focus["class_fields"] or {})]
        if uncovered:
            issues.append(
                _issue(
                    seq(),
                    CATEGORY_RISK_CALC,
                    "risk",
                    "info",
                    ["risk_result.assessment.factor_scores"],
                    f"【{hazard_key} 差异化检查】灾种重点观测字段 {uncovered} 未进入因子得分表"
                    "（不构成计算错误判定，仅提示核查因子处理合理性）",
                    [f"因子键={sorted(factor_scores.keys())}", f"重点字段={focus['key_fields']}"],
                    confidence=0.4,
                    requires_human_review=True,
                )
            )
