"""
Risk Agent 的共享状态定义。

【输入口径】
  Risk Agent 不再接受"别的 Agent 的 State 平铺包"，只接受统一契约 `assess.v1` 的
  证据面（`AssessEvidence`）拆出来的几样东西：

      task            ← 任务描述（由入口从请求生成，只作 LLM 解释的上下文）
      hazard_type     ← 平台灾种键（必填；缺失/不支持走 insufficient_data）
      observations    ← `FactorObservation` dump 列表（按因子名取用）
      site_profile    ← `SiteProfile` dump 或 None（画像属性已作为 quality="site"
                        的观测并入 observations，这里只作区域透传）
      data_provenance ← 请求证据的出处 dump（输出节点改写为本次研判口径）

  这样外部实现与平台内环读的是同一份证据结构；`hits` 只是规则命中，不是观测，
  不再冒充事件骨架喂给因子引擎。

【节点契约】
  assess   读 task / hazard_type / observations / retry_count / validation_result
           写 risk_factors / calculation_result / evidence / risk_result / uncertainties
  validate 读 risk_result / evidence / uncertainties
           写 validation_result / retry_count
  output   读 risk_result / evidence / validation_result / calculation_result / data_provenance
           写 risk_result（最终交付物）/ status
"""

from typing import TypedDict


class RiskAgentState(TypedDict):
    """Risk Agent 的全局状态，贯穿整个评估流程"""

    # === 输入（入口填充，节点只读）===
    task: str  # 任务描述：给 LLM 解释用的上下文
    hazard_type: str  # ★ 必填 ★ 平台灾种键（见 aegis.domain.enums.HazardType）
    region_code: str  # 区域编码（证据包的 region_code，透传给因子信封）
    observations: list  # FactorObservation dump 列表（平台证据包的观测面）
    site_profile: dict | None  # SiteProfile dump（可空）
    data_provenance: dict  # 请求证据的 DataProvenance dump（输出节点改写为本次研判口径）
    max_retries: int  # 最大允许重试次数（默认 3）

    # === 评估阶段（risk_assess 节点写入）===
    risk_factors: list  # 识别出的风险因素列表
    calculation_result: dict  # 确定性因子引擎信封（P/T/R/覆盖率/规则命中）
    evidence: list  # 证据链，每条绑定来源
    risk_result: dict  # 评估初稿；output 节点覆盖为最终交付物
    uncertainties: list  # 不确定性说明

    # === 验证阶段（risk_validate 节点写入）===
    validation_result: dict  # 验证报告；graph 条件路由读它

    # === 控制流 ===
    retry_count: int  # 已重试次数（每次校验建议 retry +1）
    next: str  # 备用路由标识（实际路由以 validation_result 为准）
    error_message: str  # 工具/模型/数据异常文本

    # === 输出（risk_output 节点写入）===
    status: str  # success / uncertain / insufficient_data / failed
