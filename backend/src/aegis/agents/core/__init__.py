"""智能体层的共享内核：平台内环与灾种模块共用的横切件。

这一层是 **L3 智能体层自己的契约面**（与 `agentkit.py`、`capabilities.py` 同级），
不是 L4 服务层的一部分——灾种模块的边界门禁是白名单制（`test_hazards_boundaries.py`），
内核若留在 `services/llm/` 就会逼灾种包做 L3→L4 倒挂依赖。

共享的判据很简单：**"两个 provider 都要用、且用起来不该有两套行为"的**归这里；
**"只有某家厂商才有"的**留在各自的 provider 适配器里。

- `breaker.py`：熔断状态机（带冷却窗口）与失败分类口径——两侧共用一份实现。
- `prompts.py`：提示词注册表（版本化真源）。
- `ledger.py`：LLM 调用台账与时延埋点。
- `meta.py`：调用元数据与 trace_id 的跨线程上下文。
- `providers.py`：provider 错误分类策略。
"""

from __future__ import annotations

from aegis.agents.core.breaker import (
    LlmBreaker,
    LlmUnavailableError,
    State,
    is_transport_failure,
)

__all__ = ["LlmBreaker", "LlmUnavailableError", "State", "is_transport_failure"]
