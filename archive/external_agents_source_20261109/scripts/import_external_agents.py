"""把外部智能体模块并入主线的一次性搬入脚本（用完即弃，留在仓里是为了可复现）。

做四件事：
1. 复制外部仓库的实现目录到 `backend/src/aegis/agents_ext/`（去掉测试文件与运行时产物）；
2. 把顶层包名 import 改成包内绝对路径（`from agents.` → `from aegis.agents_ext.` 等）；
3. 删掉 `sys.path.insert` 引导块（包内不需要，且它会把仓库根塞进 sys.path）；
4. 空的 `tools/` 占位目录不并入（并进去就是"看起来有工具其实没有"的空壳）。

只用标准库，跑法：`python backend/scripts/import_external_agents.py <外部仓库路径>`。
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

#: 顶层包名 → 包内绝对路径前缀。左边是她仓库里的顶层名，右边是并入后的绝对路径。
IMPORT_REWRITES: tuple[tuple[str, str], ...] = (
    ("from agents.", "from aegis.agents_ext."),
    ("import agents.", "import aegis.agents_ext."),
    ("from agents import", "from aegis.agents_ext import"),
    ("from prompts.", "from aegis.agents_ext.prompts."),
    ("from workflow.", "from aegis.agents_ext.workflow."),
    ("from hazard_registry import", "from aegis.agents_ext.hazard_registry import"),
    ("from llm import", "from aegis.agents_ext.llm import"),
    ("import config", "from aegis.agents_ext import config"),
    # 函数体内的延迟导入形态（原实现为了避开循环依赖而写在函数里）
    ("from config import", "from aegis.agents_ext.config import"),
    # **字符串形态**的动态导入（`importlib.import_module("agents.risk_agent.tools.x")`）：
    # 文本替换要连引号里的包名一起改，否则运行时报 No module named 'agents'。
    ('"agents.', '"aegis.agents_ext.'),
    ("'agents.", "'aegis.agents_ext."),
)

#: `sys.path.insert` 引导块：从 `_ROOT = ...` 到 `sys.path.insert(...)` 那一行（含空行）。
BOOTSTRAP_RE = re.compile(
    r"^(?:_ROOT\s*=\s*str\(Path\(__file__\)\.resolve\(\)\.parents\[\d+\]\)\n"
    r"if _ROOT not in sys\.path:\n"
    r"\s*sys\.path\.insert\(0, _ROOT\)\n)",
    re.MULTILINE,
)

#: 顶层 `tools/`（三个 0 字节空占位）整体不搬——并进去就是"看起来有工具其实没有"的空壳。
#: agent 内部的 `tools/` 有实质内容（数据工具、风险规则引擎），**必须**搬。
#: `memory/` 是反馈记忆的运行时 JSON（会被测试写），不进仓。
SKIP_DIRS = {"__pycache__", "memory"}
SKIP_FILE_PATTERNS = ("test_*.py", "e2e_pipeline_test.py", "*.pyc")


def rewrite_source(text: str) -> str:
    """把顶层包名 import 改成包内绝对路径，并去掉 sys.path 引导块。"""
    for old, new in IMPORT_REWRITES:
        text = text.replace(old, new)
    text = BOOTSTRAP_RE.sub("", text)
    # 引导块删掉后 `sys` / `Path` 可能变成未使用——但它们在别处常有用途，
    # 这里不做激进清理，交给 ruff 的 F401 判定（未使用则报出来，逐个处理）。
    return text


def copy_tree(src: Path, dst: Path, *, skip_dirs: frozenset[str] = frozenset()) -> int:
    """复制目录（跳过 skip_dirs 里的目录名与 SKIP_FILE_PATTERNS 里的文件），返回复制的文件数。"""
    copied = 0
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if rel.parts and rel.parts[0] in skip_dirs:
            continue
        if path.is_dir():
            continue
        if any(path.match(pattern) for pattern in SKIP_FILE_PATTERNS):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".py":
            target.write_text(rewrite_source(path.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
        else:
            shutil.copy2(path, target)
        copied += 1
    return copied


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    source = Path(sys.argv[1]).resolve()
    repo = Path(__file__).resolve().parents[2]
    target = repo / "backend" / "src" / "aegis" / "agents_ext"
    if not (source / "agents").is_dir():
        print(f"源目录不像外部仓库（缺 agents/）：{source}")
        return 2

    # 实现主体：六个 agent 目录
    for agent in ("data_agent", "feedback_agent", "manager_agent", "risk_agent", "simulation_agent", "warning_agent"):
        copied = copy_tree(source / "agents" / agent, target / agent, skip_dirs=frozenset(SKIP_DIRS))
        print(f"{agent}: {copied} files")
    # 公共件
    for name in ("prompts", "workflow"):
        copied = copy_tree(source / name, target / name, skip_dirs=frozenset({"__pycache__"}))
        print(f"{name}: {copied} files")
    for name in ("config.py", "llm.py", "hazard_registry.py"):
        src_file = source / name
        if src_file.exists():
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_text(rewrite_source(src_file.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
            print(f"{name}: 1 file")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
