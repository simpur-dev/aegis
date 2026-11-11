"""LLM 调用的元数据上下文：跨线程把"这次调用是谁、哪条提示词、哪个 trace"带进 provider。

## 为什么需要它

provider 适配器（平台网关 / 灾种客户端）在**底层**才知道这次调用的结果——时延、token、
校验态、成功还是失败。但"这次调用属于哪个 task、用了哪版提示词、归哪个智能体"是
**调用方**才知道的。两者在不同地方，就只能靠"把参数一路传下去"，而 langchain 的
`invoke()` 没有位置放这些。

于是用 ContextVar：调用方在发起调用前 `set` 一次，provider 在底层 `get` 一次。

## trace_id 的老问题

`aegis/logging.py` 里早就有 `trace_id_var`（JSON 日志的 `trace_id` 字段靠它），但
**全仓没有任何一处 `.set()`**——于是那条字段从来是空的，"一次灾害事件全链路可归因"
这句话没有兑现。两侧一起补上：

- 平台侧：网关入口按请求的 trace_id set；
- 灾种侧：本体 `handle()` 按总线信封的 trace_id set，`pool.run_blocking` 的
  `copy_context()` 把它穿透进图节点。

本模块的 `trace_id_var` 就是 `aegis/logging.py` 的**同一个** ContextVar（不是副本），
两处 set 都对同一份生效。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from aegis.logging import trace_id_var

__all__ = ["CallMeta", "llm_call_meta", "trace_id_var"]


@dataclass(frozen=True, slots=True)
class CallMeta:
    """一次 LLM 调用的归属信息。"""

    task: str = ""
    prompt_version: str | None = None
    trace_id: str | None = None
    agent_id: str | None = None
    structured: bool = False


#: 当前协程链上的调用元数据。provider 在底层读它。
#: 默认 None 而不是空元数据：ContextVar 的默认值在**创建时**求值一次、之后一直共享
#: 同一个实例，可变默认值在这里是隐患（哪怕 CallMeta 是 frozen 的）。
call_meta_var: ContextVar[CallMeta | None] = ContextVar("aegis_llm_call_meta", default=None)


@contextmanager
def llm_call_meta(
    *,
    task: str = "",
    prompt_version: str | None = None,
    trace_id: str | None = None,
    agent_id: str | None = None,
    structured: bool = False,
) -> Iterator[CallMeta]:
    """在调用期间挂上元数据（退出即还原）。

    `trace_id` 一并 set 到 `trace_id_var`：这样同一次调用里的日志行也带 trace_id，
    台账与日志用同一个归因号。
    """
    meta = CallMeta(
        task=task,
        prompt_version=prompt_version,
        trace_id=trace_id,
        agent_id=agent_id,
        structured=structured,
    )
    token = call_meta_var.set(meta)
    trace_token = trace_id_var.set(trace_id) if trace_id is not None else None
    try:
        yield meta
    finally:
        if trace_token is not None:
            trace_id_var.reset(trace_token)
        call_meta_var.reset(token)


def current_call_meta() -> CallMeta:
    """当前调用元数据（没有就是空元数据）。"""
    return call_meta_var.get() or CallMeta()


def now_mono() -> float:
    """单调钟读数（provider 侧量时延用）。"""
    return time.monotonic()
