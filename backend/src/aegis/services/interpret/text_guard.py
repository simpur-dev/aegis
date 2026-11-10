"""数字防幻觉闸（§3.4 / §4.7 的"同款"，单一真源）。

LLM 只允许改写措辞：一旦生成文本出现"事实里没有的数字"，整段弃用——绝不"就近采信"或编数。
`assistant` 的值班秘书与未来的 execute/推演文案共用这一份判定，避免两处阈值漂移（两张皮）。

口径：
- 数字既包括阿拉伯数字 `\\d+(?:\\.\\d+)?`，也包括中文数字（"三""一百二十三"），中文先归一化为
  阿拉伯数值，因此无法靠"换写法"绕过，也不会把等价的"三 / 3"误判成新数字；
- 按出现次数（multiset）而非集合比较：把事实数字替换成另一个已存在的数字（计数超出）也能抓到。
事实基准由调用方给定（预警正文/读数/等级等已实测的来源文本）；`invented_numbers` 非空即视为幻觉，
调用方据此降级或拒收。
"""

from __future__ import annotations

import re
from collections import Counter

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_CN_NUMBER_RE = re.compile(r"[零一二三四五六七八九十百千万亿两]+")

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def _cn_to_int(text: str) -> int:
    """把常见中文数字转成整数（支持十/百/千/万/亿、两、零），如"一百二十三" -> 123。"""
    total = 0
    section = 0
    digit = 0
    for ch in text:
        if ch in _CN_UNITS:
            section += (digit or 1) * _CN_UNITS[ch]
            digit = 0
        elif ch in ("万", "亿"):
            section += digit
            total += section * (10000 if ch == "万" else 100000000)
            section = 0
            digit = 0
        else:
            digit = _CN_DIGITS[ch]
    return total + section + digit


def _number_counts(text: str) -> Counter[str]:
    """文本中各规范数值的出现次数：阿拉伯数字原样、中文数字归一化为阿拉伯数值。"""
    counts: Counter[str] = Counter()
    for token in _NUMBER_RE.findall(text or ""):
        counts[token] += 1
    for phrase in _CN_NUMBER_RE.findall(text or ""):
        counts[str(_cn_to_int(phrase))] += 1
    return counts


def number_tokens(text: str) -> set[str]:
    """文本中出现过的数字（去重）。"""
    return set(_number_counts(text))


def invented_numbers(generated: str, facts: str) -> set[str]:
    """生成文本里出现次数超过事实基准的数字集合；非空即幻觉。"""
    return set(_number_counts(generated) - _number_counts(facts))
