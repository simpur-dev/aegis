"""仓库卫生：测试模块的重名会让一半用例"看起来跑了"其实根本没被收集。

今晚真实踩到：新加的 `tests/unit/test_load_profile.py` 与已有的
`tests/perf/test_load_profile.py` 同名，而 tests 目录不是包（无 `__init__.py`），
pytest 按模块名导入 → `import file mismatch` 直接把整个会话打断（或更早：静默少收一个文件）。
这条检查比 `__init__.py` 更便宜，且能在有人新增重名文件时就失败。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]


def test_test_module_basenames_are_unique_across_the_suite() -> None:
    names = [path.name for path in TESTS_ROOT.rglob("test_*.py") if "__pycache__" not in path.parts]
    assert len(names) >= 60, f"没量到真实测试树：{len(names)} 个文件"
    duplicated = sorted(name for name, count in Counter(names).items() if count > 1)
    assert not duplicated, f"测试模块重名（pytest 会 import file mismatch）: {duplicated}"
