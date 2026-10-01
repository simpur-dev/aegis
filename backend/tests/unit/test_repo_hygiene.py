"""仓库卫生：测试模块的重名会让一半用例"看起来跑了"其实根本没被收集。

今晚真实踩到：新加的 `tests/unit/test_load_profile.py` 与已有的
`tests/perf/test_load_profile.py` 同名，而 tests 目录不是包（无 `__init__.py`），
pytest 按模块名导入 → `import file mismatch` 直接把整个会话打断（或更早：静默少收一个文件）。
这条检查比 `__init__.py` 更便宜，且能在有人新增重名文件时就失败。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

TESTS_ROOT = Path(__file__).resolve().parents[1]


def test_test_module_basenames_are_unique_across_the_suite() -> None:
    names = [path.name for path in TESTS_ROOT.rglob("test_*.py") if "__pycache__" not in path.parts]
    assert len(names) >= 60, f"没量到真实测试树：{len(names)} 个文件"
    duplicated = sorted(name for name, count in Counter(names).items() if count > 1)
    assert not duplicated, f"测试模块重名（pytest 会 import file mismatch）: {duplicated}"


def test_scripts_imports_are_backed_by_pythonpath() -> None:
    """有测试 `import scripts.x` 时，pytest 必须配了 pythonpath。

    今晚真实事故：`tests/unit/test_accuracy_replay_report.py` 收集期 ModuleNotFoundError，
    而 tests 不是包、`scripts` 也不在 sys.path 上——收集阶段报错会让**整场** pytest 中断，
    不是少跑一个文件。这条检查守住那行配置不被顺手删掉。
    """
    importers = [
        path.name
        for path in TESTS_ROOT.rglob("test_*.py")
        if "__pycache__" not in path.parts and "from scripts." in path.read_text(encoding="utf-8")
    ]
    if not importers:
        pytest.fail("没有任何测试 import scripts：这条门禁失去了对象，配置该连同用例一起清理")
    config = (TESTS_ROOT.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'pythonpath = ["."]' in config, f"这些测试依赖 scripts 包但没有 pythonpath：{importers}"
