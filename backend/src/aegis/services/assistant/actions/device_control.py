"""L4 设备控制动作：把一条设备指令交给既有的白名单外呼腿。

不另写一份 HTTP 客户端、也不绕过工作流节点的策略：`OutboundCaller.refusal_reason` 就是
`workflow/nodes.py::_device_control` 用的那个方法/协议/主机白名单判据。助手只是把
`device / action / command_url` 三个由操作者给出的值送进去，不合成 URL、不猜设备。
白名单关闭或拒绝时明确回 503/403，绝不"看起来执行成功了"。
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aegis.domain.operator import OperatorRole
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


class DevicePort(Protocol):
    """动作侧看见的外呼腿形状，由 `OutboundCaller` 实现。"""

    @property
    def enabled(self) -> bool: ...

    def refusal_reason(self, method: str, url: str) -> str | None: ...

    async def __call__(self, method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]: ...


class DeviceControlParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    action: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_-]*$")
    command_url: str = Field(min_length=1, max_length=500)


class DeviceControl(Action):
    spec = ActionSpec(
        name="device.control",
        title="下发设备指令",
        domain=ActionDomain.DEVICE,
        required_role=OperatorRole.LEAD,
        params=DeviceControlParams,
        needs_confirm=True,
        writes=True,
        previewable=True,
        dual_review=True,
        example="设备控制 device=valve-01 action=open command_url=https://device.internal/control",
    )

    def __init__(self, caller: DevicePort | None) -> None:
        self._caller = caller

    async def resolve(self, params: BaseModel, ctx: ActionContext) -> ResolvedTargets:
        assert isinstance(params, DeviceControlParams)
        return ResolvedTargets((TargetRef("device", params.device, params.action),))

    async def preview(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ImpactPreview:
        assert isinstance(params, DeviceControlParams)
        caller = self._caller
        reason = caller.refusal_reason("POST", params.command_url) if caller is not None else "外呼腿未装配"
        return ImpactPreview(
            targets=targets.items,
            irreversible=(f"「{params.action}」一旦下发到 {params.device} 不可撤销",),
            warnings=(f"目标主机必须通过 AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS 白名单：{reason or '已放行'}",),
        )

    async def execute(self, targets: ResolvedTargets, params: BaseModel, ctx: ActionContext) -> ActionResult:
        assert isinstance(params, DeviceControlParams)
        caller = self._caller
        if caller is None or not caller.enabled:
            raise ActionError(
                "E_DEVICE_UNAVAILABLE",
                "设备外呼腿未启用（AEGIS_WORKFLOW_HTTP_ALLOWED_HOSTS 为空），拒绝下发",
            )
        reason = caller.refusal_reason("POST", params.command_url)
        if reason is not None:
            raise ActionError("E_ACTION_NOT_ALLOWED", f"设备指令被策略拒绝：{reason}")
        body = {"device": params.device, "action": params.action}
        try:
            response = await caller("POST", params.command_url, body)
        except Exception as exc:
            raise ActionError("E_ACTION_EXECUTION_FAILED", f"设备 {params.device} 指令下发失败：{str(exc)[:200]}") from exc
        return ActionResult(
            data={"device": params.device, "action": params.action, "response": response},
            summary=f"设备 {params.device} 已下发「{params.action}」",
        )
