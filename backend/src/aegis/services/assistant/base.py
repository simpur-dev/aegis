"""助手动作的基类与上下文：一次动作请求的全部输入输出都在这里定义。

为什么是抽象基类而不是"一堆可空 callable 的 dataclass"（旧的 `AssistantActions`）：
可空回调把"依赖是什么"藏进了运行时的 `getattr`，新增一个动作要在 dataclass、container、
测试替身三处同步；换成构造注入的类之后，`WarningList(store)` 这一行就说明了它需要什么，
缺依赖会在装配期直接失败，而不是等用户问到时才回一句"当前形态不可执行"。

本模块不 import 任何具体服务：动作实现只依赖构造时注入的对象，测试可以喂 fake。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel

from aegis.domain.operator import Operator, OperatorRole


class ActionDomain(StrEnum):
    """动作域。`assert_consistent` 要求每个域至少一个动作，防止拼错的域被静默忽略。"""

    QUERY = "query"
    WARNING = "warning"
    TASK = "task"
    WORKFLOW = "workflow"
    RULE = "rule"
    DRILL = "drill"
    NOTIFY = "notify"
    DEVICE = "device"


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """一个动作的说明书。注册表是唯一真源，能力面 / 权限判定 / 测试都读它。"""

    name: str
    title: str
    domain: ActionDomain
    required_role: OperatorRole
    params: type[BaseModel]
    needs_confirm: bool = False
    #: 是否写动作。读动作允许匿名（审计记 unknown），写动作必须有有效席位。
    writes: bool = False
    reversible: bool = False
    idempotent: bool = True
    previewable: bool = False
    #: 高安全部署打开 `AEGIS_ASSISTANT_DUAL_REVIEW_ENABLED` 时，本动作是否需要两名不同席位复核。
    #: 只标在四类不可逆动作上（发布预警/批量/改规则/设备控制）；默认关着时这个字段完全不生效，
    #: 不会给日常处置平白加一道审批——升级方案 §3.1 规则 3 与 D5 都要求这一点。
    dual_review: bool = False
    example: str = ""


@dataclass(frozen=True, slots=True)
class TargetRef:
    """被操作对象的最小引用：只放能点回去的标识与展示名。"""

    kind: str
    id: str
    label: str = ""


@dataclass(frozen=True, slots=True)
class ResolvedTargets:
    """目标解析结果。空列表表示"这个动作没有具体目标"（如列表查询）。"""

    items: tuple[TargetRef, ...] = ()

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.items)

    def __bool__(self) -> bool:
        return bool(self.items)


@dataclass(frozen=True, slots=True)
class ImpactPreview:
    """影响预览：批准之前用户必须看到"会动到什么、影响多少人、能不能撤回"。"""

    targets: tuple[TargetRef, ...] = ()
    audience_count: int = 0
    channels: tuple[str, ...] = ()
    irreversible: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "targets": [{"kind": item.kind, "id": item.id, "label": item.label} for item in self.targets],
            "audience_count": self.audience_count,
            "channels": list(self.channels),
            "irreversible": list(self.irreversible),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ActionResult:
    """执行结果：`data` 给机器（前端渲染 / 后续步骤），`summary` 给叙述层做确定性措辞。"""

    data: dict[str, Any] = field(default_factory=dict)
    summary: str = ""


@dataclass(frozen=True, slots=True)
class ActionContext:
    """一次动作请求的上下文。读动作允许匿名（`operator=None`，审计记 unknown）。"""

    session_id: str
    utterance: str = ""
    operator: Operator | None = None
    space_id: str | None = None
    approved: bool = False
    idempotency_key: str | None = None
    now: float = field(default_factory=time.monotonic)


class ActionError(RuntimeError):
    """带错误码的动作失败。HTTP 映射只在 API 层做一次（见 `executor.ERROR_STATUS`）。"""

    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


class Action(ABC):
    """动作基类。子类只需要声明 `spec` 并实现 `execute`。"""

    spec: ClassVar[ActionSpec]

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        """把参数解析成具体目标。只读、无副作用；默认认为不需要目标。"""
        return ResolvedTargets()

    async def preview(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ImpactPreview:
        """影响预览。默认只有目标清单；写动作应覆盖并给出受众/通道/不可逆项。"""
        return ImpactPreview(targets=targets.items)

    @abstractmethod
    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        """执行动作。业务拒绝抛 `ActionError`（业务错误码透传），其它异常由执行器归一。"""

    async def revoke(self, record: dict[str, Any], ctx: ActionContext) -> ActionResult:
        """撤销一次动作（已执行的还要做业务补偿）。只有 `spec.reversible` 为真的动作需要实现。

        `record` 是动作台账里的那一行：补偿要读它的 `result`（例如改级前的 `before_level`），
        只拿一个 `action_id` 是补偿不了的。
        """
        raise ActionError("E_ACTION_NOT_REVERSIBLE", f"动作 {self.spec.name} 不支持撤销")
