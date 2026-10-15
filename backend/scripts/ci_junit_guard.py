"""CI 守卫：一条绿色 job 必须证明自己真跑了用例，而不是全体 skip 之后的静默通过。

动机（2026-10-15 的实测）：`integration-postgres` 的 pytest 步骤整段只用 7s，而公开 API 只能证明
"junit 文件存在、没有失败"——环境变量名一旦打错，`skipif` 会让整批用例跳过、job 照绿，这种假话
当时从外部根本证不伪。产物计数由 job 自己带上，判据就落在仓里。
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

_SUMMARY_HEADER = (
    "### 计数守卫（产物 / tests / skipped / ran / failures / errors）\n\n"
    "| 产物 | tests | skipped | ran | failures | errors |\n"
    "| --- | --- | --- | --- | --- | --- |\n"
)


def publish(target: Path, tests: int, skipped: int, failures: int, errors: int) -> None:
    """把计数发到 CI 外部真能读到的地方——两条通道的可达性不一样，这点是实测出来的。

    `$GITHUB_STEP_SUMMARY` 只渲染在 Actions 页面上：运行 186 里 `check-run.output.summary`
    匿名读回是空串（长度 0），所以它不算"公开可读"。`::notice::` / `::error::` 落成 annotation，
    而 annotation 列表接口不要令牌——上一批文档写过"计数从此公开可读"，那句当时没验，这里按实测改道。
    """
    ran = tests - skipped
    sink = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if sink:
        path = Path(sink)
        body = path.read_text(encoding="utf-8") if path.exists() else ""
        header = "" if body.strip() else _SUMMARY_HEADER
        path.write_text(
            f"{body}{header}| `{target}` | {tests} | {skipped} | {ran} | {failures} | {errors} |\n",
            encoding="utf-8",
            newline="\n",
        )
    if os.environ.get("GITHUB_ACTIONS") == "true":
        level = "notice" if ran > 0 else "error"
        print(f"::{level} title=计数守卫::tests={tests} skipped={skipped} ran={ran} failures={failures} errors={errors}")


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
    # 判负之前先把计数发出去：红的时候正是最需要看见这五个数的时候
    publish(target, tests, skipped, failures, errors)
    if ran <= 0:
        print(
            "红：这条 job 一条用例都没真跑（0 收集或全体 skip）——绿不代表测过东西",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
