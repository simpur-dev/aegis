"""试运行（dry-run）：独立注册表 + 外部动作的"只记账不执行"包装。

试运行要回答的是"这张图现在跑一遍会得到什么"，不是"现在通知谁"。
所以两条边界必须同时成立：
- 不写任何持久面：不建版本、不写草稿、不进实例账本（实例仍是 `_instances` 之外的一份快照）；
- 不产生外部副作用：通知/外呼/设备控制/预警发布/链路段一律换成"本来会调用什么"的回执。

LLM 那几类（识别/定级/推演/生成）照常执行——它们读的是知识库与案例，正是试运行要看的结果；
把这一层也打成桩，试运行就只剩一张空图。真机上的耗时读数也来自这一层。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from aegis.workflow.nodes import WorkflowServices
from aegis.workflow.ports import PreviewStorePort

PREVIEW_LIMIT = 8
PREVIEW_TTL_SECONDS = 30 * 60.0
#: 试运行入参上限：预览跑的是真 LLM 腿，一份几十兆的 payload 只能换来一次超时。
MAX_PREVIEW_PAYLOAD_BYTES = 256 * 1024


class PreviewLimitReached(Exception):
    """试运行同时在册的数量到顶：先看旧的、或等它过期。"""


def new_preview_id(prefix: str = "pvp_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class InMemoryPreviewStore(PreviewStorePort):
    """进程内试运行结果表：上限 + TTL，取用一次不删（前端可能刷新后再看）。"""

    def __init__(
        self,
        *,
        limit: int = PREVIEW_LIMIT,
        ttl_seconds: float = PREVIEW_TTL_SECONDS,
        clock: Any = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("试运行上限必须 ≥ 1")
        if ttl_seconds <= 0:
            raise ValueError("试运行 TTL 必须 > 0")
        self._limit = int(limit)
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}

    def __len__(self) -> int:
        return len(self._items)

    @property
    def limit(self) -> int:
        return self._limit

    def prune(self, now_monotonic: float) -> int:
        expired = [key for key, (at, _) in self._items.items() if now_monotonic - at > self._ttl]
        for key in expired:
            self._items.pop(key, None)
        return len(expired)

    def ensure_room(self) -> None:
        """在真跑之前先占位：等 LLM 跑完再说"满了"是白花一次调用。"""
        now = self._clock()
        self.prune(now)
        if len(self._items) >= self._limit:
            raise PreviewLimitReached(f"试运行同时在册已达上限 {self._limit} 份（每份保留 {int(self._ttl)} 秒），请先查看或等待过期")

    def put(self, preview_id: str, payload: dict[str, Any]) -> None:
        now = self._clock()
        self.prune(now)
        if len(self._items) >= self._limit:
            raise PreviewLimitReached(f"试运行同时在册已达上限 {self._limit} 份")
        self._items[preview_id] = (now, payload)

    def get(self, preview_id: str) -> dict[str, Any] | None:
        self.prune(self._clock())
        item = self._items.get(preview_id)
        return item[1] if item is not None else None


def dry_run_services(base: WorkflowServices | None = None) -> WorkflowServices:
    """在原装配之上，只替换"会产生外部副作用"的那几条腿。

    `notify` 的返回值本来就被 handler 丢掉，所以这里返回 None；节点侧靠 `ctx.dry_run`
    在自己的产出里说明"没发出去"。
    """
    source = base if base is not None else WorkflowServices()

    async def would_notify(_payload: dict[str, Any]) -> None:
        return None

    async def would_http_call(method: str, url: str, _body: Any = None) -> dict[str, Any]:
        return {"dry_run": True, "would_call": "api_call", "method": method, "url": url}

    async def would_publish(payload: dict[str, Any]) -> dict[str, Any]:
        channels = payload.get("channels")
        count = len(channels) if isinstance(channels, list) else 0
        return {
            "dry_run": True,
            "would_call": "warning_publish",
            # 预览里"会投递到几个通道"是这张图的分支依据；不虚构已投递，号码只说明意图。
            "delivered": 0,
            "would_deliver_to": count,
        }

    async def would_chain_stage(stage: str, _payload: dict[str, Any]) -> dict[str, Any]:
        return {"dry_run": True, "would_call": "chain_stage", "stage": stage, "branch": ""}

    return WorkflowServices(
        # 智能腿照旧：试运行要看的就是它们的真实产出
        telemetry_query=source.telemetry_query,
        identify=source.identify,
        assess=source.assess,
        simulate=source.simulate,
        generate_warning=source.generate_warning,
        collect_feedback=source.collect_feedback,
        # 副作用腿全部换成回执
        publish_warning=would_publish,
        run_chain_stage=would_chain_stage,
        notify=would_notify,
        http_call=would_http_call,
    )


__all__ = [
    "PREVIEW_LIMIT",
    "PREVIEW_TTL_SECONDS",
    "InMemoryPreviewStore",
    "PreviewLimitReached",
    "dry_run_services",
    "new_preview_id",
]
