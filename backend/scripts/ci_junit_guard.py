"""CI 守卫：一条绿色 job 必须证明自己真跑了用例，而不是全体 skip 之后的静默通过。

动机（2026-10-15 的实测）：`integration-postgres` 的 pytest 步骤整段只用 7s，而公开 API 只能证明
"junit 文件存在、没有失败"——环境变量名一旦打错，`skipif` 会让整批用例跳过、job 照绿，这种假话
当时从外部根本证不伪。产物计数由 job 自己带上，判据就落在仓里。
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def counts(path: Path) -> tuple[int, int, int, int]:
    """取 testsuite 的 (tests, skipped, failures, errors)。文件缺失或结构不对一律算不合格。"""
    root = ET.parse(path).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    if suite is None:
        raise ValueError("junit 里没有 testsuite 节点")
    return (
        int(suite.get("tests") or 0),
        int(suite.get("skipped") or 0),
        int(suite.get("failures") or 0),
        int(suite.get("errors") or 0),
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：ci_junit_guard.py <junit.xml>", file=sys.stderr)
        return 2
    target = Path(argv[1])
    if not target.is_file():
        print(f"红：产物不存在，这条 job 没有可核对的计数：{target}", file=sys.stderr)
        return 1
    try:
        tests, skipped, failures, errors = counts(target)
    except (ValueError, ET.ParseError) as exc:
        print(f"红：产物读不出计数：{exc}", file=sys.stderr)
        return 1
    ran = tests - skipped
    print(f"[junit 守卫] {target}: tests={tests} skipped={skipped} ran={ran} failures={failures} errors={errors}")
    if ran <= 0:
        print(
            "红：这条 job 一条用例都没真跑（0 收集或全体 skip）——绿不代表测过东西",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
