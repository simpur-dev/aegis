"""
Risk Agent 的共享状态定义。

【与 Data Agent State 的对照】（方便你理解为什么长这样）
  机制完全相同：节点读 state、return 局部合并（LangGraph 标准玩法）。
  字段分组也对齐你的 Data State：输入 / 阶段产出 / 控制流 / 最终输出。

【★ 交接契约 —— 哪些字段给谁 ★】

  ▶ Manager 启动 Risk 时【必须填】的输入字段：
      task          ← 任务描述
      data_result   ← ★★ 放 Data Agent 的最终完整 State（平铺结构）★★
                       即你 data_output_node return 出来的那一份：
                       {processed_data:{events:[...]}, data_quality:{...},
                        missing_data:[...], status:..., error_message:...}
      max_retries   ← 可选，不填默认 3（对齐你 Data 路由里的 MAX_RETRIES=3）

  ▶ Risk 内部流转、Manager 不用管的字段：
      risk_factors, calculation_result, evidence, uncertainties,
      validation_result, retry_count, next, error_message

  ▶ Risk 产出、【提供给下游】的字段：
      risk_result   ← ★ 给 Simulation Agent 读的结构化风险结果 ★
      status        ← ★ 给 Manager 判断流程是否继续 ★
"""

from typing import TypedDict


class RiskAgentState(TypedDict):
    """Risk Agent 的全局状态，贯穿整个评估流程"""

    # === 输入（由 Manager 填充，RiskAgent拿到就有，只读，一般不修改）===
    task: str  # 任务描述：Manager给过来的任务，例如："评估西藏波密地区泥石流风险"
    hazard_type: str  # ★ 必填 ★ 灾种（标准键或明确中文名，如 "debris_flow"/"泥石流"）；
    # risk_assess_node 归一化后写回标准键（无效灾种会走 insufficient_data）
    # 标准键定义见根目录 hazard_registry.py（本项目不研究地震）
    data_result: dict  # 从Data Agent交接过来的数据！DataAgent处理完数据之后，把它的state平铺打包放到这里。
    # 里面包含该灾种清洗后的观测记录、质量信息等数据，是风险评估的原始素材。

    # === 评估阶段（risk_assess 节点写入）===
    risk_factors: list  # 识别出的风险因素列表，LLM在这里找出有哪些风险
    calculation_result: dict  # 确定性计算工具结果：调用tools里risk_calculation_tool算出来的数值结果
    # 比如缺口、风险量化指标，纯数学计算，不是LLM猜测
    evidence: list  # 证据链，每条证据绑定来源
    # 后面验证节点会核对证据是否真实
    risk_result: dict  # LLM生成的风险结论初稿
    # 👉注意注释：assess节点写初稿，risk_output节点覆盖，变成交付给Manager的最终版本
    uncertainties: list  # 不确定性说明：哪些地方数据不全、信息模糊，没法百分百确定，后续输出节点会写到报告里

    # === 验证阶段（risk_validate 节点写入）===
    validation_result: dict  # 验证报告！**这是控制路由最核心的字段！**
    # graph的条件分支会读取这个字典，判断下一步怎么走：
    # 例如 {"is_valid":True/False,"reason":"证据不足/计算错误"}
    # 如果校验失败，就回到评估节点重试；校验成功，进入输出节点

    # === 控制流（循环、异常、路由控制）===
    retry_count: int  # 当前已经重试多少次，每次校验失败+1，用来限制循环
    max_retries: int  # 最大允许重试次数（注释默认3次），防止无限循环死循环
    next: str  # 备用路由标识（注释说了：实际路由以validation_result为准，镜像DataAgent写法，保留字段）
    error_message: str  # 错误信息：工具报错、LLM调用失败、数据异常时，在这里记录报错文本

    # === 输出（risk_output节点写入，交给上游Manager Agent）===
    status: str  # 给Manager的状态标记，4种固定取值：
    # success：评估成功
    # uncertain：评估结果存在不确定性
    # insufficient_data：数据不足无法评估
    # failed：评估执行失败
