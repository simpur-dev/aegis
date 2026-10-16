"""数字防幻觉闸（§3.4 / §4.7 的"同款"，单一真源）。

LLM 只允许改写措辞：一旦生成文本出现"事实里没有的数字"，整段弃用——绝不"就近采信"或编数。
`assistant` 的值班秘书与未来的 execute/推演文案共用这一份判定，避免两处阈值漂移（两张皮）。

口径：数字 = `\\d+(?:\\.\\d+)?`（整数与小数都算）。事实基准由调用方给定（预警正文/读数/等级等
已实测的来源文本）；`invented_numbers` 非空即视为幻觉，调用方据此降级或拒收。
"""

from __future__ import annotations

import re

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def number_tokens(text: str) -> set[str]:
    return set(_NUMBER_RE.findall(text or ""))


def invented_numbers(generated: str, facts: str) -> set[str]:
    """生成文本里出现、但事实里没有的数字集合；非空即幻觉。"""
    return number_tokens(generated) - number_tokens(facts)
