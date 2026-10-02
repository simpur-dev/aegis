"""JetStream 消费者名与队列名的合法化规则（唯一真源）。

为什么要有这个模块： durable 消费者名是**服务端状态的键**，nats-py 对它做严格校验
（`_validate_consumer_name`：`.` `>` `*` 空格 `:` 等一律非法），而我们的名字里会带上
`agent_id`（形如 `perceive.mock01`）。部署形态下这不是"名字不好看"，而是应用启动直接失败：
`ValueError: nats: invalid consumer name: 'd_perceive_perceive.mock01'`（2026-10-02 实测，
`AEGIS_BUS_BACKEND=nats` 起不来）。内存总线不校验，所以这条缺陷只在真总线上出现。

规则必须确定、可复算：改名会让服务端把旧的 durable 消费者当成"另一个消费者"，
未确认的消息因此留在旧键下。所以这里只做字符替换，并且**只在真的改写过时**追加一段
原名的短哈希——`a.b` 与 `a_b` 若不区分，会静默落到同一个消费者上共享投递状态，
那是比启动失败更难查的一类问题。
"""

from __future__ import annotations

import re
from hashlib import sha1

# JetStream 允许的字符集（保守取并集：字母数字、下划线、短横线）。
_INVALID = re.compile(r"[^A-Za-z0-9_-]+")
# 服务端对名字长度没有硬上限，但名字会进监控面板与日志；超限时保留可辨识的前缀 + 哈希。
_MAX_LENGTH = 96


def consumer_name(*parts: str) -> str:
    """把若干片段拼成一个合法的消费者/队列名。

    空片段被丢掉（调用方常传可选的实例号）；全部为空时返回 `"anon"` 而不是空串——
    空名字在 nats-py 里等价于"不要 durable 订阅"，静默降级成无状态订阅是不可接受的。
    """
    raw = "_".join(part for part in parts if part)
    if not raw:
        return "anon"
    safe = _INVALID.sub("_", raw).strip("_")
    if not safe:
        return f"anon_{sha1(raw.encode()).hexdigest()[:8]}"
    if safe != raw or len(safe) > _MAX_LENGTH:
        digest = sha1(raw.encode()).hexdigest()[:8]
        safe = f"{safe[: _MAX_LENGTH - 9].rstrip('_')}_{digest}"
    return safe
