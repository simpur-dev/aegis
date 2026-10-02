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


def test_every_test_module_parses() -> None:
    """每个测试模块都要能被 `ast.parse`。

    今晚两次真实打断：中文用例名里混进空格（`def test_区划代码 CHECK 与站点表同口径`）
    造成语法错，而语法错发生在**收集期**——pytest 整场中断，不是少跑一个文件。
    这条把"哪个文件第几行"直接报出来，省掉一轮"全套为什么不绿"的排查。
    """
    import ast

    paths = [path for path in sorted(TESTS_ROOT.rglob("test_*.py")) if "__pycache__" not in path.parts]
    assert len(paths) >= 60, f"没量到真实测试树：{len(paths)} 个文件"
    broken: list[str] = []
    for path in paths:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            broken.append(f"{path.name}:{exc.lineno} {exc.msg}")
    assert not broken, "测试树不可收集（会打断整场 pytest）：" + "；".join(broken)


REPO_ROOT = TESTS_ROOT.parents[1]


def _lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]


def _ignore_patterns() -> list[str]:
    return [line for line in _lines(REPO_ROOT / ".dockerignore") if line and not line.startswith("#")]


def _copy_sources() -> list[str]:
    """Dockerfile 里真正被 COPY 的宿主路径（`--from=` 的多阶段复制不算）。"""
    sources: list[str] = []
    for line in _lines(REPO_ROOT / "deploy" / "Dockerfile"):
        if not line.startswith("COPY") or "--from=" in line:
            continue
        tokens = [token for token in line.split()[1:] if not token.startswith("--")]
        if len(tokens) >= 2:
            sources.append(tokens[0])
    return sources


def test_后端构建上下文排掉凭据与宿主产物() -> None:
    """没有 .dockerignore 时，`docker build` 先把 1.7GB+（含 .env 与 node_modules）压进管道。

    慢只是表面；.env 进了上下文，就可能被 CI 缓存层与构建记录留下痕。
    """
    patterns = _ignore_patterns()
    for required in (".env", ".env.*", "**/.venv/", "frontend/node_modules/", "**/__pycache__/", "backend/data/"):
        assert required in patterns, f".dockerignore 少了 {required}"
    # 模板本身要留：`cp .env.example .env` 是文档里的第一步，把它连同 .env 一起排除会让镜像构建期读不到键名
    assert "!.env.example" in patterns


def test_dockerignore不排除Dockerfile真正COPY的东西() -> None:
    """致命错法只有一种：把 `backend` 或 `contracts` 整目录列进忽略表——镜像于是静默少一层源码。

    带通配的模式不在这里做 glob 复刻（那是 BuildKit 的口径），这条只挡"整目录被忽略"。
    """
    sources = _copy_sources()
    assert {"backend", "contracts"} <= set(sources), f"Dockerfile 的 COPY 源变了，这条检查要看一眼：{sources}"
    literal = [p for p in _ignore_patterns() if not p.startswith("!") and "*" not in p and "?" not in p]
    for source in sources:
        blocked = [p for p in literal if source == p.rstrip("/") or source.startswith(p.rstrip("/") + "/")]
        assert not blocked, f"COPY {source} 会被 .dockerignore 的 {blocked} 挡掉，镜像里就没有这部分代码"
