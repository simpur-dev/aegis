#!/usr/bin/env bash
# 外部智能体模块并入主线的**一条命令**（可复现）。
#
#   bash backend/scripts/import_external_agents_all.sh <外部仓库路径>
#
# 五步：
#   1. 搬实现（顶层包名 import → 包内绝对路径；去 sys.path 引导；跳过测试与运行时产物）
#   2. 搬测试（她的 109 条用例 → backend/tests/unit/agents_ext/；mock 构造器回实现包内）
#   3. 后处理（把"必要的改造"固化：4 处真实缺陷 + assert False 改 raise）
#   4. ruff 自动修与格式化（机械等价改动：import 排序、类型注解现代化）
#   5. 补齐缺失的 __init__.py（她的顶层目录是 PEP 420 隐式命名空间包，进包内需显式）
#
# 跑完之后必须过：pytest tests/unit/agents_ext、ruff check、mypy src。
set -euo pipefail

SRC="${1:?用法: import_external_agents_all.sh <外部仓库路径>}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "== 1/5 搬实现 =="
# 只删**外部来源**的那些目录，不整个 rm -rf：平台侧在这个包里加的文件
# （adapter.py 适配层等）必须留下——它们不来自外部仓库，重跑搬入不该抹掉。
for d in data_agent feedback_agent manager_agent risk_agent simulation_agent warning_agent prompts workflow; do
  rm -rf "$REPO/backend/src/aegis/agents_ext/$d"
done
rm -f "$REPO/backend/src/aegis/agents_ext/config.py" \
      "$REPO/backend/src/aegis/agents_ext/llm.py" \
      "$REPO/backend/src/aegis/agents_ext/hazard_registry.py"
python "$REPO/backend/scripts/import_external_agents.py" "$SRC"

echo "== 2/5 搬测试 =="
rm -rf "$REPO/backend/tests/unit/agents_ext"
python "$REPO/backend/scripts/import_external_agents_tests.py" "$SRC"

echo "== 3/5 后处理（固化必要改造）=="
python "$REPO/backend/scripts/postprocess_external_agents.py" "$REPO"
python "$REPO/backend/scripts/postprocess_external_agents_types.py" "$REPO"

echo "== 4/5 ruff 自动修与格式化 =="
cd "$REPO/backend"
uv run python -m ruff check src/aegis/agents_ext tests/unit/agents_ext --fix --unsafe-fixes >/dev/null || true
uv run python -m ruff format src/aegis/agents_ext tests/unit/agents_ext >/dev/null

echo "== 5/5 补 __init__.py =="
for d in "" prompts workflow \
         data_agent data_agent/nodes data_agent/tools \
         feedback_agent feedback_agent/nodes feedback_agent/tools \
         manager_agent manager_agent/nodes \
         risk_agent risk_agent/nodes risk_agent/tools \
         simulation_agent simulation_agent/nodes simulation_agent/tools simulation_agent/models \
         warning_agent warning_agent/nodes warning_agent/tools; do
  touch "$REPO/backend/src/aegis/agents_ext/$d/__init__.py"
done

echo "完成。下一步："
echo "  cd backend && uv run python -m pytest tests/unit/agents_ext -q"
echo "  cd backend && uv run python -m ruff check src tests && uv run python -m mypy src"
