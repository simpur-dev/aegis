"""property 用例的样本数唯一出口：深度留在装饰器上，CI 用环境变量统一封顶。

为什么不是 hypothesis profile：逐用例 `@settings(max_examples=…)` 会覆盖注册的
profile（REPORT 2026-11-20 实测过），所以要么逐个改用例、要么把字面量换成这里。
封顶语义用 `min` 而不是乘法：深的用例（120 条）被压到帽、浅的冒烟用例（5 条）
保持原样——CI 省的是"深度的长尾"，不削稀有路径的覆盖。
"""

from __future__ import annotations

import os


def examples(default: int) -> int:
    """`max_examples=examples(120)`：无环境变量时原样返回；设了 `AEGIS_HYP_EXAMPLES_MAX` 则封顶。"""
    cap = os.environ.get("AEGIS_HYP_EXAMPLES_MAX", "")
    if not cap:
        return default
    try:
        limit = int(cap)
    except ValueError:
        return default
    return max(1, min(default, limit))
