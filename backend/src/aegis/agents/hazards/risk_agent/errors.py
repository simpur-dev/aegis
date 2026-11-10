"""研判实现拒绝给出结论时抛出的类型化错误。

`AgentKit._on_request` 按 `error_code` / `retryable` / `error_detail` 三个属性把异常
转成 `kind=error` 的回执——只有走 error 通道，网关才记 `outcome=error`、平台才走
规则降级轨。把失败塞进正常响应的嵌套 dict 会被网关当成功记账（协同成功率虚高）。
"""

from __future__ import annotations

from typing import Any

from aegis.domain.assess import AssessErrorCode


class AssessRejected(Exception):
    """本次研判不能产出契约结论（数据不足 / 灾种不支持 / 内部失败）。"""

    def __init__(
        self,
        code: AssessErrorCode | str,
        message: str,
        *,
        retryable: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = str(code)
        self.message = message
        self.retryable = retryable
        self.error_detail = detail or {}


__all__ = ["AssessRejected"]
