"""架构依赖铁律的守卫（《课题6_项目架构设计》v3.0 §5.2，铁律 3 / 4）。

规则不是愿景：这里用 AST 把源码的 import 与调用事实量出来，违反即红。
取证基线（2026-10-03，v3.0 编写前对 src/aegis 全量 grep 复核）：

- ``domain/`` 对 aegis.* 的 import 为 0——domain 是枚举与契约镜像的唯一真源，不依赖任何包；
- ``bus/ storage/ services/ workflow/ observability/`` 对可选腿包
  （persistence/analytics/knowledge/retrieval/edge/connectors）的 import 为 0；
- ``pipeline/`` 对可选腿的**模块级** import 只有 ``aegis.knowledge.provider``
  （KnowledgeProvider 协议 + CaseMatch 数据类）；检索层引用只出现在
  TYPE_CHECKING 与函数体内（延迟导入，见 chain.py 头部注释）；
- 可选腿的 ``build_*`` 构建函数，生产调用点只有 ``container.py`` / ``integrations.py``
  与 ``backend/scripts`` 运维入口（ingest_cases 等走同一装配点），此外只允许腿包内部自用。

改这些规则的唯一方式是先改架构文档、再改这里——两者必须一起过评审。
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src" / "aegis"
SCRIPTS = BACKEND / "scripts"

OPTIONAL_LEGS = {"persistence", "analytics", "knowledge", "retrieval", "edge", "connectors"}
KERNEL_CORE = {"bus", "storage", "services", "workflow", "observability"}
ASSEMBLY_FILES = {"container.py", "integrations.py"}


def _iter_py(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _aegis_imports(tree: ast.Module, own_package: str) -> tuple[set[tuple[int, str]], set[tuple[int, str]]]:
    """返回 (模块级, 非模块级) 的 (行号, 完整模块名)，只收跨包引用。

    col_offset == 0 视为模块级；TYPE_CHECKING 块与函数体内的导入都算非模块级（延迟）。
    own_package 是文件所在顶层包名，包内引用一律不算。
    """
    module_level: set[tuple[int, str]] = set()
    deferred: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules = [node.module]
        else:
            continue
        for mod in modules:
            parts = mod.split(".")
            if len(parts) < 2 or parts[0] != "aegis" or parts[1] == own_package:
                continue
            bucket = module_level if node.col_offset == 0 else deferred
            bucket.add((node.lineno, mod))
    return module_level, deferred


def _build_functions_defined_in_legs() -> dict[str, Path]:
    builders: dict[str, Path] = {}
    for leg in sorted(OPTIONAL_LEGS):
        for path in _iter_py(SRC / leg):
            for node in ast.walk(_parse(path)):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("build_"):
                    builders.setdefault(node.name, path)
    return builders


def test_铁律4_domain零依赖() -> None:
    violations = []
    for path in _iter_py(SRC / "domain"):
        module_level, deferred = _aegis_imports(_parse(path), "domain")
        for lineno, mod in sorted(module_level | deferred):
            violations.append(f"{path.relative_to(SRC)}:{lineno} -> {mod}")
    assert not violations, "domain 是唯一真源，不得依赖任何 aegis 包（连延迟导入也不行）:\n" + "\n".join(violations)


def test_铁律3_内核环不import可选腿() -> None:
    violations = []
    for pkg in sorted(KERNEL_CORE):
        for path in _iter_py(SRC / pkg):
            module_level, deferred = _aegis_imports(_parse(path), pkg)
            for lineno, mod in sorted(module_level | deferred):
                if mod.split(".")[1] in OPTIONAL_LEGS:
                    violations.append(f"{path.relative_to(SRC)}:{lineno} -> {mod}")
    assert not violations, (
        "内核环（bus/storage/services/workflow/observability）不得 import 可选腿："
        "腿的能力一律经 Protocol 注入（装配点在 container/integrations）:\n" + "\n".join(violations)
    )


def test_铁律3_pipeline对可选腿只允许知识协议模块与延迟导入() -> None:
    violations = []
    for path in _iter_py(SRC / "pipeline"):
        module_level, _deferred = _aegis_imports(_parse(path), "pipeline")
        for lineno, mod in sorted(module_level):
            top = mod.split(".")[1]
            if top not in OPTIONAL_LEGS:
                continue
            if mod == "aegis.knowledge.provider":
                continue  # KnowledgeProvider 协议 + CaseMatch：链路经注入使用，不算 I/O 依赖
            violations.append(f"{path.relative_to(SRC)}:{lineno} 模块级 import {mod}（只允许 aegis.knowledge.provider，其余必须延迟导入）")
    assert not violations, "\n".join(violations)


def test_铁律3_腿的构建函数只从装配点调用() -> None:
    builders = _build_functions_defined_in_legs()
    assert builders, "没扫到任何腿内 build_* 函数——扫描器坏了，这条守卫在空转"
    violations = []
    scanned = [*_iter_py(SRC), *_iter_py(SCRIPTS)]
    for path in scanned:
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(BACKEND)
        for node in ast.walk(_parse(path)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in builders):
                continue
            defined_in = builders[node.func.id]
            same_leg = defined_in.parent == path.parent
            if path.name in ASSEMBLY_FILES or SCRIPTS in path.parents or same_leg:
                continue
            violations.append(f"{rel}:{node.lineno} 调用 {node.func.id}（定义于 {defined_in.relative_to(BACKEND)}）")
    assert not violations, (
        "可选腿的 build_* 只能从 container.py / integrations.py / scripts 运维入口（或腿包内部）调用，"
        "散落调用点会让'内核不依赖腿的 I/O'失守:\n" + "\n".join(violations)
    )
