"""
证据整理工具（灾种无关，适配因子引擎信封）

【本文件角色】把"因子得分 + 组合指数 + 保护规则命中 + 数据覆盖"汇总成带来源的证据。
              纯整理，不判断，不改变任何数值。
【可观测性设计】每条证据标 type：observation(观测/计算)/derived(规则推导)/
                limitation(局限)，方便 validate 审查与后续追溯。
【对外契约】
  提供给：risk_assess_node.py → State["evidence"] → validate/output
"""


def risk_evidence_tool(assessment: dict, hazard_limitations: list | None = None) -> list:
    """从因子评估信封组装证据链（灾种无关）。

    参数:
        assessment: hazard_factor_assessment_tool 返回的统一信封
        hazard_limitations: 该灾种"尚未具备的能力"清单（来自 hazard_risk_registry）

    返回:
        证据列表，每条含 statement + source + type
    """
    evidence = []

    # 1. 组合指数与分组得分（观测/计算事实）
    if assessment.get("assessment_status") == "scored":
        evidence.append(
            {
                "statement": (
                    f"孕灾条件得分 P={assessment.get('P')}，"
                    f"动态触发得分 T={assessment.get('T')}，"
                    f"组合指数 R={assessment.get('R')}（{R_FORMULA_REF}，"
                    f"项目原型自拟候选组合公式，非规范条款、非任务书公式，"
                    f"未经历史数据验证）"
                ),
                "source": f"factor_engine({assessment.get('engine_version')})",
                "type": "observation",
            }
        )
    else:
        evidence.append(
            {
                "statement": ("数据覆盖不足，未计算组合指数；原因: " + "；".join(assessment.get("insufficient_reasons", []))),
                "source": f"factor_engine({assessment.get('engine_version')})",
                "type": "limitation",
            }
        )

    # 2. 因子得分明细（观测事实，逐因子）
    for _key, spec in (assessment.get("factor_scores", {}) or {}).items():
        z = spec.get("z")
        if z is None:
            evidence.append(
                {
                    "statement": f"因子[{spec.get('label')}]缺失：{spec.get('note', '无观测值')}"
                    f"（记录覆盖率 {spec.get('record_coverage', 0):.0%}）",
                    "source": "factor_engine",
                    "type": "limitation",
                }
            )
        else:
            raw = spec.get("raw_value")
            unit = spec.get("unit") or ""
            evidence.append(
                {
                    "statement": (
                        f"因子[{spec.get('label')}] 原始值={raw}{unit}，"
                        f"归一化 z={z}，权重 {spec.get('weight')}"
                        f"（{spec.get('weight_kind', 'default')}）"
                    ),
                    "source": "factor_engine",
                    "type": "observation",
                }
            )

    # 3. 保护规则命中（推导，明确标注 MOCK）
    for hit in assessment.get("rule_hits", []):
        evidence.append(
            {
                "statement": (
                    f"保护规则命中 {hit['rule_id']}：{hit['description']}"
                    f"（证据字段 {hit['evidence_field']}={hit['evidence_value']}，"
                    f"建议等级 {hit.get('level_suggest', '')}）"
                ),
                "source": f"hazard_rule({hit.get('validation_status', 'mock')})",
                "type": "derived",
                "caveat": assessment.get("disclaimer", ""),
            }
        )

    # 4. 数据局限性（诚实声明）
    evidence.append(
        {
            "statement": "当前数据仅含灾种观测要素本身（Mock 构造），无人口/建筑/承灾体暴露度信息，评估为事件固有危险性而非综合灾害风险",
            "source": "data_interface",
            "type": "limitation",
        }
    )
    for lim in hazard_limitations or []:
        evidence.append({"statement": lim, "source": "capability_statement", "type": "limitation"})

    return evidence


# 引用常量（避免循环 import：从 factor_engine 取公式声明文本）
from aegis.agents.hazards.risk_agent.tools.factor_engine import R_FORMULA_EXPRESSION as R_FORMULA_REF  # noqa: E402
