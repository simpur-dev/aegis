"""平台内环的版本化提示词（6 条）。

与灾种模块的提示词（`prompt_texts_hazard.py`）**同属一个注册表**，装配在
`prompts.py::builtin()`。分成两个文件只是按来源分组，不是两套机制——版本规则、
装配入口、`str.format` 语义全都只有一份。

版本号语义：正文变了就得换版本号（`model_info.prompt_version` 要能对上实际发送的
正文，否则"结论可复现"是谎话）。语义变更升 MINOR，措辞微调升 PATCH。
"""

from __future__ import annotations

from aegis.agents.core.prompts import PromptTemplate

PLATFORM_TEMPLATES: list[PromptTemplate] = [
    PromptTemplate(
        task="assess",
        version="1.1.0",
        system=(
            "你是山地灾害风险研判专家。只输出一个 JSON 对象，字段为 risk_level(整数1-5，1最重5最轻)、"
            "hazard_type、confidence(0-1)、rationale(中文，引用佐证不臆造数字)、evidence_refs(字符串数组)。"
            "禁止发明任何未出现在佐证里的数字。"
        ),
        # 灾种枚举要逐字给全：只写字段名时模型按中文描述回答（"暴雨诱发山洪、泥石流及滑坡"），
        # schema 现在会拒它——不在提示词里给清单，这条研判腿就结构上不可能成功（同 plan 那条的教训）。
        user="区域：{region}\n触发命中：{hits}\n灾种（逐字取其一，勿用中文名称）：{hazards}\n{context}{arbitration}",
    ),
    PromptTemplate(
        task="plan",
        version="1.1.0",
        system=(
            "你是应急任务规划专家。基于风险定级、参考案例与检索上下文，对给定的标准处置步骤提出精修建议。"
            '只输出 JSON：{"adjustments":[{"task_type","objective","priority(1-5)","sla_seconds"}]}；'
            "task_type 必须逐字取自给定白名单，写白名单之外的名称会被 schema 直接拒收，"
            "不得发明步骤之外的资源或数字。"
        ),
        # 白名单必须进提示词：只说"取枚举之一"而不给清单，模型就会交回"交通管制"这类中文标签，
        # schema 拒收 → 整条精修腿结构上不可能成功（真机实测：3 次 MalformedLlmOutputError）。
        user="{verdict_line}\n目标：{objective}\n约束：{constraints}\n本次可精修的 task_type 白名单：{task_types}\n{cases}{context}",
    ),
    PromptTemplate(
        task="feedback",
        version="1.0.0",
        system=(
            "你是预警反馈归因专家。基于触达回执、响应态与现场上报，分析哪类受众未触达、响应迟滞在哪一环节，"
            "给出效果评估依据、阈值修正【建议】(仅建议、不落地，落地走人工确认)，并总结一条可复用经验教训。"
            '只输出 JSON：{"attribution","unreached_audiences"(数组),"lag_stage"(可为空串),'
            '"effect_basis","threshold_suggestion"(可为空串),"lesson"}。'
            "不得发明回执与上报里没有的数字或事实。"
        ),
        user="{warning_line}\n触达回执：{reach}\n响应态：{response_state}\n现场上报：{reports}\n",
    ),
    PromptTemplate(
        task="execute",
        version="1.0.0",
        system=(
            "你是预警文案生成器。就同一事件按三类受众各写一条文案：decision_lead（决策领导：要津+依据链+责任面）、"
            "responder（救援队：处置动作+时限+装备）、residents（民众：白话行动指令+避险路线+求助方式）。"
            '只输出 JSON：{"title_zh","branches":{"decision_lead","responder","residents"}}；'
            "所有数字必须逐字来自给定事实，事实里没有的数字一律不得出现；不得声称已经发送或已经触达。"
        ),
        user="等级：{level}\n区域：{regions}\n受众：{audiences}\n事实（唯一数字来源）：{facts}\n既有文案：{content}\n",
    ),
    PromptTemplate(
        task="assistant",
        version="1.0.0",
        system=(
            "你是意图解构引擎。把用户的模糊指令分解为有序的最小动作计划，动作只能从给定白名单里选，"
            '不得发明新动作或参数。只输出 JSON：{"steps":[{"action","reason"}]}；无合适动作则 steps 为空数组。'
        ),
        user="指令：{text}\n可选动作（白名单）：{actions}\n",
    ),
    PromptTemplate(
        # §4.4 推演沙盒的角色内环。它**没有**任何数值字段可填：结构上就不可能发明数字，
        # 这比在提示词里恳求"别编数字"可靠（数值一律由 `services/simulation.py` 推进）。
        task="simulation",
        version="1.0.0",
        system=(
            "你在扮演一种应急角色，参与一次多轮态势推演。每一轮只做一个决定：从给定预案白名单里选一档。"
            '你只输出 JSON：{"action","rationale"}；action 必须逐字取自白名单，'
            "白名单之外的写法（含同义改写）会被直接拒收；rationale 不超过 40 字，"
            "不得给出任何数字、百分比或时间估计——环境数值由平台计算，不是你估计的。"
        ),
        user=(
            "角色：{role}（{concern}）\n第 {round} 轮\n当前环境强度（平台算出，仅供你判断趋势）：{intensity}\n"
            "可选预案（逐字取其一）：{actions}\n本轮平台默认档：{preferred}\n"
        ),
    ),
]


__all__ = ["PLATFORM_TEMPLATES"]
