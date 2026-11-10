"""灾种模块本体的类型化错误。

`AgentKit._on_request` 按 `error_code` / `retryable` / `error_detail` 三个属性把异常
转成 `kind=error` 的回执——只有走 error 通道，网关才记 `outcome=error`、平台才走降级轨。
把失败塞进正常响应的嵌套 dict 会被网关当成功记账（协同成功率虚高），那比报错更糟。

研判本体另有一层 `AssessRejected`（`risk_agent/errors.py`）：它带的是 `assess.v1`
契约自己的错误码（`E_ASSESS_*`），那是**对外契约**的一部分；本模块的错误码走平台的
总线错误分类（`ErrorCode`），是**总线面**的。两套码各管一面，不互相冒充。
"""

from __future__ import annotations

from typing import Any

from aegis.errors import ErrorCode


class HazardRejected(Exception):
    """灾种本体本次给不出结论（输入不合、依赖缺失、内部失败）。"""

    def __init__(
        self,
        code: ErrorCode | str,
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


__all__ = ["HazardRejected"]
