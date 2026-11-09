"""写动作的频控与熔断（设计 §4.8）。

两件事分开放：频控防"手快"，熔断防"腿坏"。
- 单会话写动作 10 次/分钟：超出后读动作照常，写动作提示等待；
- 同一动作 60 秒内失败 3 次：熔断 60 秒，期间该动作直接拒绝执行。

时钟可注入，测试不必真的熬夜等一分钟。两者都只记时间戳，不记参数与指令原文——
留痕在 `audit_ops`，这里不复制一份隐私面更宽的日志。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

#: 一次解析出的目标超过这个数就要求拆分：批量动作一条指令最多动 50 个对象。
MAX_TARGETS = 50


@dataclass(slots=True)
class WriteRateLimiter:
    """单会话写动作滑窗计数。只保留窗口内的命中，过期条目在访问时顺手清掉。"""

    max_per_window: int = 10
    window_seconds: float = 60.0
    clock: Callable[[], float] = time.monotonic
    _hits: dict[str, deque[float]] = field(default_factory=dict)

    def allow(self, key: str) -> bool:
        now = self.clock()
        bucket = self._hits.setdefault(key, deque())
        while bucket and now - bucket[0] >= self.window_seconds:
            bucket.popleft()
        if len(bucket) >= self.max_per_window:
            return False
        bucket.append(now)
        return True

    def forget(self, key: str) -> None:
        """会话被回收时一并丢掉计数，避免键随会话号无限增长。"""
        self._hits.pop(key, None)


@dataclass(slots=True)
class FailureBreaker:
    """按动作名的失败熔断。成功一次即清零：断的是这条腿，不是整个助手。"""

    failure_threshold: int = 3
    window_seconds: float = 60.0
    cooldown_seconds: float = 60.0
    clock: Callable[[], float] = time.monotonic
    _failures: dict[str, deque[float]] = field(default_factory=dict)
    _open_until: dict[str, float] = field(default_factory=dict)

    def remaining(self, action: str) -> float:
        """还剩多少秒解除熔断；0 表示当前可以尝试。"""
        now = self.clock()
        until = self._open_until.get(action, 0.0)
        if until > now:
            return until - now
        if until:
            self._open_until.pop(action, None)
        return 0.0

    def record_failure(self, action: str) -> None:
        now = self.clock()
        bucket = self._failures.setdefault(action, deque())
        while bucket and now - bucket[0] >= self.window_seconds:
            bucket.popleft()
        bucket.append(now)
        if len(bucket) >= self.failure_threshold:
            self._open_until[action] = now + self.cooldown_seconds
            bucket.clear()

    def record_success(self, action: str) -> None:
        self._failures.pop(action, None)
        self._open_until.pop(action, None)
