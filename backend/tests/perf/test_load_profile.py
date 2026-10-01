"""压测档案的守卫测试：locustfile 不能被"改了阈值没人知道"或"打向 404"腐化掉。

这里刻意**不在本进程内 import locust**：locust 导入时会 `monkey.patch_all()`，
在已经加载过 ssl/urllib3 的 pytest 进程里会触发 gevent 的 RecursionError（Python 3.12 实测）。
所以结构检查走 AST，真实加载检查交给子进程跑 `locust --check-config`。

守的三件事：
1. 档案结构完整（两个 User 类、任务齐全），现场跑压测时才发现语法错就太晚了；
2. 阈值来自 `Settings` 而不是被改回常数——否则压测口径与告警口径漂移成两套；
3. 档案里打到的每个端点都存在于 OpenAPI，避免压测把 404 跑成"绿色"。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aegis.api.app import create_app
from aegis.config import Settings

LOCUSTFILE = Path(__file__).resolve().parents[1] / "load" / "locustfile.py"


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    assert LOCUSTFILE.exists(), f"压测档案缺失: {LOCUSTFILE}"
    return ast.parse(LOCUSTFILE.read_text(encoding="utf-8"), filename=str(LOCUSTFILE))


def _class_names(tree: ast.Module) -> list[str]:
    return [node.name for node in tree.body if isinstance(node, ast.ClassDef)]


def _module_assignments(tree: ast.Module) -> dict[str, ast.expr]:
    """含 AnnAssign：READ_ENDPOINTS 写成 `tuple[str, ...]`，只看 Assign 会漏掉它。"""
    out: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            out[node.target.id] = node.value
    return out


def _names_used(node: ast.AST) -> list[str]:
    """表达式里出现的属性名与字符串字面量：阈值检查要抓 `settings.sla_schedule_ms` 这种属性引用。"""
    out: list[str] = []
    for item in ast.walk(node):
        if isinstance(item, ast.Attribute):
            out.append(item.attr)
        elif isinstance(item, ast.Constant) and isinstance(item.value, str):
            out.append(item.value)
    return out


def test_file_parses_and_exposes_two_user_classes(tree: ast.Module) -> None:
    names = _class_names(tree)
    assert "AegisReadUser" in names and "AegisDrillUser" in names, names


def test_read_user_declares_read_tasks(tree: ast.Module) -> None:
    read_user = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AegisReadUser")
    tasks = [node.name for node in read_user.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")]
    assert len(tasks) >= 4, tasks


def test_thresholds_are_derived_from_settings_not_hardcoded(tree: ast.Module) -> None:
    """阈值一旦写回常数，压测与告警就会漂移成两套口径。"""
    assignments = _module_assignments(tree)
    for key, field in (("SCHEDULE_SLA_MS", "sla_schedule_ms"), ("SYNC_SLA_MS", "sla_sync_ms")):
        value = assignments.get(key)
        assert value is not None, f"{key} 必须存在"
        assert field in _names_used(value), f"{key} 必须取 Settings.{field}，不得写常数"

    text = LOCUSTFILE.read_text(encoding="utf-8")
    for literal in ("= 2000", "= 3000", "= 2_000", "= 3_000"):
        assert literal not in text, f"locustfile 里出现硬编码阈值 {literal!r}"


def test_settings_carry_the_official_indicator_values() -> None:
    """阈值本身也要钉住：这两个数字就是考核指标，被改动时这里必须响。"""
    settings = Settings(env="test")
    assert settings.sla_schedule_ms == 2_000
    assert settings.sla_sync_ms == 3_000


def test_every_read_endpoint_exists_in_the_openapi_document(tree: ast.Module) -> None:
    assignments = _module_assignments(tree)
    read_endpoints = [
        item.value for item in ast.walk(assignments["READ_ENDPOINTS"]) if isinstance(item, ast.Constant) and isinstance(item.value, str)
    ]
    assert read_endpoints, "READ_ENDPOINTS 不能为空，否则读侧压测形同虚设"

    document: dict[str, Any] = create_app(Settings(env="test")).openapi()
    paths = set(document.get("paths", {}))
    for endpoint in read_endpoints:
        route = endpoint.split("?", 1)[0]
        assert route in paths, f"压测打向不存在的端点 {route}（404 会让指标失真）"


def test_drill_endpoint_is_declared() -> None:
    document: dict[str, Any] = create_app(Settings(env="test")).openapi()
    assert "/api/v1/drill/run" in document.get("paths", {})


def test_read_profile_only_issues_gets(tree: ast.Module) -> None:
    """读侧用户不得发写请求：压测改写平台状态会让时延样本混进写抖动。"""
    read_user = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AegisReadUser")
    calls = [node for node in ast.walk(read_user) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)]
    verbs = {node.attr for node in calls if node.attr in {"get", "post", "put", "patch", "delete"}}
    assert verbs == {"get"}, verbs


@pytest.mark.slow
def test_profile_loads_under_locust() -> None:
    """真加载只能在干净解释器里做：locust 会 monkey patch ssl，混进 pytest 进程会 RecursionError。"""
    probe = (
        "import importlib.util, locust;"
        f"spec = importlib.util.spec_from_file_location('aegis_locustfile', r'{LOCUSTFILE}');"
        "assert spec and spec.loader;"
        "module = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(module);"
        "assert module.AegisReadUser.wait_time and module.AegisDrillUser.wait_time;"
        "print('loaded')"
    )
    completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, (completed.stdout + completed.stderr)[-2000:]
    assert "loaded" in completed.stdout
