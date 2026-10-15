"""链路根跨度 `hazard_chain` 的共用记账：两条编排路径（固定顺序 / 工作流）写同一棵树。

为什么不各写一遍：根跨度上落的属性（`aegis.chain.ok/stages/degradations/errors`）与
"有 errors 判故障、否则有降级判 degraded"的口径就是链路对第三方（OTLP 后端、告警规则）的
自述。两处各写迟早分叉，而分叉的表现是"同一件事在两条路径上告警行为不同"——最难查的那类。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aegis.observability import instrumentation
from aegis.pipeline.result import ChainResult


async def run_under_root(
    trace: str,
    body: Callable[[], Awaitable[ChainResult]],
    *,
    region: str,
    reading_count: int,
    intake: str,
) -> ChainResult:
    """在 `hazard_chain` 根跨度下跑一次链路，并按结果落状态。

    根跨度在活动上下文里激活，因此其中的阶段跨度与网关协同事务跨度自动成为它的子节点，
    整棵树的 traceID 由契约 trace_id 确定性映射而来（见 observability/instrumentation.py）。
    计时口径不在这里：各段时延由段实现里的 `tracer.span(...)` 落账，SLA 判定以时延账本为准。
    """
    async with instrumentation.span(
        "hazard_chain",
        trace_id=trace,
        kind="server",
        span_attrs={"aegis.region_code": region, "aegis.readings": reading_count, "aegis.intake": intake},
    ) as root:
        result = await body()
        # 事件 ID 在链路内部才生成，回填到根跨度上，取证时可用 event_id 或 trace_id 双向检索
        instrumentation.set_attributes(
            root,
            span_attrs={
                "aegis.event_id": result.event_id,
                "aegis.chain.ok": result.ok,
                "aegis.chain.stages": len(result.stages),
                "aegis.chain.degradations": len(result.degradations),
                "aegis.chain.errors": len(result.errors),
            },
        )
        if result.errors:
            instrumentation.mark_error(root, "；".join(result.errors)[:500], code="chain_errors")
        elif result.degradations:
            instrumentation.mark_degraded(root, "；".join(result.degradations)[:500])
        return result
