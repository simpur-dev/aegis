"""智能助手（服务层）：动作注册表 + 统一执行器 + 对话编排。

与 `services/interpret/` 的分工：`interpret` 只留上报文本解析与数字闸（与助手无关的通用能力），
助手自己的意图、动作、叙述全部在这里。这样"改一个助手动作"不会牵动上报链路。
"""

from aegis.services.assistant.base import (
    Action,
    ActionContext,
    ActionDomain,
    ActionError,
    ActionResult,
    ActionSpec,
    ImpactPreview,
    ResolvedTargets,
    TargetRef,
)
from aegis.services.assistant.executor import ERROR_STATUS, ExecutionOutcome, Executor
from aegis.services.assistant.intent import Intent, IntentClassifier
from aegis.services.assistant.registry import ActionRegistry
from aegis.services.assistant.service import AssistantEvent, AssistantService

__all__ = [
    "ERROR_STATUS",
    "Action",
    "ActionContext",
    "ActionDomain",
    "ActionError",
    "ActionRegistry",
    "ActionResult",
    "ActionSpec",
    "AssistantEvent",
    "AssistantService",
    "ExecutionOutcome",
    "Executor",
    "ImpactPreview",
    "Intent",
    "IntentClassifier",
    "ResolvedTargets",
    "TargetRef",
]
