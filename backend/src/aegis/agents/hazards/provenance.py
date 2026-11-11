"""灾种本体的 D10 凭证：把"这条结论由哪条提示词、哪个模型产出"写进回执信封。

## 为什么需要

D10（ADR-0007）要求 response 类结论带 `model_info`（模型 + prompt 版本），它是
"结论可复现"的物质基础。平台自建的五段内环早就填了（`agents/assess.py::_provenance`
读 `llm.model_info_for_task`），**灾种六个本体一个都没填**——8 处 `Reply()` 全裸。

## 口径

每个本体有一个**主提示词**（结论由它定），本体的 `model_info` 报的就是它：

| 本体            | 主提示词                   |
|-----------------|---------------------------|
| data            | `hazard.data_plan`         |
| risk            | `hazard.risk_explain`      |
| simulation      | `hazard.results_analysis`  |
| warning         | `hazard.warning_explain`   |
| feedback        | `hazard.feedback_suggestion` |
| manager（闭环）  | `hazard.manager_report`    |

因子引擎的版本**不在这里重复**：它已在 payload 的 `assessment.engine_version` 与
业务 `model_info` dict 里。那里是"业务模型的版本"，信封的 `model_info` 是"生成文本
的模型与提示词版本"——**同名不同层**（见 ADR-0017）。

## 为什么"模型名"可以空着

提示词版本能查到、模型名查不到时（例如 provider 未配置，这一腿走了模板兜底），
只写 `prompt_version`、`model` 留空——**不臆造模型名**。反之亦然。
"""

from __future__ import annotations

from aegis.agents.core.prompts import default_registry
from aegis.domain.messages import ModelInfo

#: 每个本体一个主提示词（结论由它定）。键是 agent_id 的中段（`assess.data01` → `data`）。
_MAIN_PROMPT: dict[str, str] = {
    "data": "hazard.data_plan",
    "factor": "hazard.risk_explain",
    "sim": "hazard.results_analysis",
    "hazard": "hazard.warning_explain",
    "audit": "hazard.feedback_suggestion",
    "loop": "hazard.manager_report",
}


def hazard_model_info(agent_id: str, *, model: str = "") -> ModelInfo | None:
    """按本体 id 产出 D10 凭证；该本体没有主提示词时如实返回 None。"""
    slot = agent_id.split(".")[-1] if "." in agent_id else agent_id
    # agent_id 形如 `assess.data01` / `execute.hazard01`：取字母前缀作为槽位。
    slot = "".join(ch for ch in slot if ch.isalpha()) or slot
    task = _MAIN_PROMPT.get(slot)
    if task is None:
        return None
    version = default_registry().version_for_task(task)
    if version is None and not model:
        return None
    return ModelInfo(model=model or None, prompt_version=version)


__all__ = ["hazard_model_info"]
