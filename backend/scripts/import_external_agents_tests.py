"""把外部智能体模块的测试一并迁入主线（一次性脚本，留仓是为了可复现）。

与 `import_external_agents.py` 同一套改写规则：顶层包名 → 包内绝对路径、去 `sys.path` 引导。
迁入位置 `backend/tests/unit/agents_ext/`：

- `test_*.py` → 平台 pytest 风格（她的原文件是"自带 __main__ runner"的手写风格，
  pytest 也能收集，本仓统一用 pytest 收口）；
- `test_data_builder.py`（她那边的 mock 数据构造器，不是测试）→ 改名为 `data_builder.py`
  放进同目录（pytest 不会把不以 test_ 开头的文件当用例收集）。

跑法：`python backend/scripts/import_external_agents_tests.py <外部仓库路径>`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_external_agents import rewrite_source

#: 她的"不是用例"的文件 → 迁入后落在**实现包内**的相对路径。
#: `test_data_builder.py` 是 mock 数据构造器（feedback 测试按原名 import 它），
#: 它是实现的一部分：落在 src/ 下、保持原文件名，pytest 的 testpaths 只收 tests/，不会被收集。
#: 注意**不能靠文件名判断**"是不是用例"——这个文件本身也叫 test_ 开头。
IMPL_SIDE_FILES: dict[str, str] = {
    "test_data_builder.py": "feedback_agent/test_data_builder.py",
}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    source = Path(sys.argv[1]).resolve()
    repo = Path(__file__).resolve().parents[2]
    target = repo / "backend" / "tests" / "unit" / "agents_ext"
    target.mkdir(parents=True, exist_ok=True)

    moved = 0
    for agent_dir in sorted((source / "agents").iterdir()):
        if not agent_dir.is_dir():
            continue
        for test_file in sorted(agent_dir.glob("test_*.py")):
            # 显式清单优先：mock 构造器落到实现包内、保持原名（feedback 测试按原名 import 它）。
            if test_file.name in IMPL_SIDE_FILES:
                out = repo / "backend" / "src" / "aegis" / "agents_ext" / IMPL_SIDE_FILES[test_file.name]
            else:
                # 迁入的测试**统一加 ext_ 前缀**：她的 test_feedback_agent.py 与平台自己的
                # tests/unit/test_feedback_agent.py 同名，pytest 的 basename 冲突会让收集失败
                # （本仓 test_repo_hygiene.py 有守卫管"测试模块重名"）。前缀是最省事也最清楚的做法。
                stem = test_file.stem
                bare = stem[5:] if stem.startswith("test_") else stem
                out = target / f"test_ext_{bare}.py"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(rewrite_source(test_file.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
            moved += 1
            print(f"{test_file} -> {out.relative_to(repo)}")

    # 顶层 e2e 脚本（不是 pytest 用例，是串联脚本）
    e2e = source / "agents" / "e2e_pipeline_test.py"
    if e2e.exists():
        out = target / "e2e_pipeline_script.py"
        out.write_text(rewrite_source(e2e.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
        print(f"{e2e} -> {out.relative_to(repo)}")
    print(f"moved {moved} test files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
