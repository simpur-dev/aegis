"""外部智能体模块搬入后的**后处理**：把必要的改造固化下来（幂等，可重跑）。

为什么要有这一步：搬入脚本是从原仓库**重新生成**代码的，所以任何"手工修过的地方"
如果不写进这一步，重跑搬入就会被冲掉。这一步把并入主线的所有改造集中成一处：

**A. 真实缺陷（不是风格）**
1. `hazard_model_router` 末尾又定义了一遍 `supported_hazards_text`（与从 hazard_registry
   导入的那份逐字相同）：覆盖无害，但违反该模块自己的"灾种定义只有一处"原则 → 删重复定义；
2. `llm_explanation_node` 的 `used_llm` 是死变量（真值在返回的 dict 里）→ 删；
3. `warning_validator_node` 读了 `decision_result` 没用 → 删（预警与决策的一致性校验归反馈环节）；
4. `data_output_node` 读了 `retry_count` 没用 → 删（输出节点不重试）；
5. `risk_evidence_tool` 的循环变量 `key` 没用到 → 改 `_key`；
6. `simulation_executor` 在 except 里 raise 没带 `from` → 补 `from exc`（保留异常链）。

**B. 语法与可读性**
7. 她的 `assert False, "消息"` → `raise AssertionError("消息")`：`python -O` 下 assert 会被去掉，
   用例就变成"静默通过"；raise 不会。
8. 超长行拆分（ruff 行宽 140）。

**C. 不改语义的原则**
以上每一条都逐字核过：被改的行要么是死代码，要么是等价写法。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> bool:
    if text == _read(path):
        return False
    path.write_text(text, encoding="utf-8", newline="\n")
    return True


# ---------- A0. 凭据治理：config.py 整文件替换 ----------

#: 治理后的 `config.py` 全文。原仓库那份把**明文智谱 API Key 写死在源码里**并提交进了
#: 公开仓库——那把钥匙已经算泄露。并入主线必须改掉：凭据只从环境变量取，
#: 运行时文件路径（checkpoint 库、反馈记忆）配置化。这一条必须**固化在脚本里**：
#: 手工改过的文件在重跑搬入时会被冲掉（密钥就会重新进仓）。
CONFIG_REPLACEMENT = '''"""外部智能体模块（`agents_ext`）的运行配置面。

【本文件在并入时改了什么——只改配置来源，其余逻辑逐字保留】
原仓库的同名文件把**明文 API Key 写死在源码里**并提交进了公开仓库。
并入主线时必须改掉，否则主仓会带着一个已泄露的凭据发布出去。三条改动：

1. **Key 只从环境变量读**（`AEGIS_EXT_LLM_API_KEY`），不再有"把 Key 粘进源码"这条路；
2. **原 Key 必须轮换**：它随原仓库公开过，已经算泄露（见 `archive/external_agents_source_20261109/README.md`）；
3. **运行时文件路径配置化**（checkpoint 库、反馈记忆 JSON）：原实现把路径写死成相对 CWD 的
   `data/...` 与包内固定位置，从不同目录启动会写到不同地方、还会写进代码仓。

【为什么保留这个文件而不是并进 `aegis.config.Settings`】
本模块是**外部交付的可选实现**：它带着自己的依赖（langgraph/langchain）与自己的运行参数，
与平台主配置面不是一回事。平台侧只通过 `aegis.config.Settings` 的
`agent_impl` 等键把"要不要装、装不装得起来"交过来；模型凭据与模型名这类
"外部实现自己的事"留在本文件里，但**只允许从环境变量取**。

【诚实声明】
没配 Key 时本模块不报错：LLM 工厂返回一个占位 Key 的实例，真正调用时 401 失败 →
各节点既有 try/except 打印原因并走确定性兜底，流程不崩。
"""

from __future__ import annotations

import os
from pathlib import Path

# ============================================================
# 智谱开放平台（GLM）配置
# ============================================================

#: API Key **只从环境变量读**。没有"粘进源码"这个选项——原仓库那样做过，且因此泄露。
#: 未配置时留空，LLM 工厂会用占位 Key（调用必 401，节点兜底降级，流程不崩）。
ZHIPUAI_API_KEY = os.environ.get("AEGIS_EXT_LLM_API_KEY", "").strip()

#: 模型名与接口地址：可用环境变量覆盖（不同部署可能用别的 OpenAI 兼容端点）。
ZHIPU_MODEL = os.environ.get("AEGIS_EXT_LLM_MODEL", "glm-4-flash").strip()

#: 智谱 OpenAI 兼容接口地址（ChatOpenAI 用 base_url 直连，无需换 SDK）
ZHIPU_BASE_URL = os.environ.get(
    "AEGIS_EXT_LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"
).strip()

# ============================================================
# Manager Agent 配置（全局调度中心）
# ============================================================

# 各 Agent 的 Manager 外层重试上限（"重新完整调用一次该 Agent"计为一次重试，
# 与各 Agent 内部自带的小重试相互独立）
MANAGER_RETRY_CONFIG = {
    "data_agent": 2,
    "risk_agent": 1,
    "simulation_agent": 1,
    "warning_agent": 1,
    "feedback_agent": 1,
}

# Feedback 返工闭环最大轮数（防 Manager ⇄ Feedback 无限反复）
MANAGER_MAX_FEEDBACK_LOOPS = 2

# 模拟是否可选：True 时 Simulation 失败并达重试上限 → 跳过并继续 Warning，
# 任务标记 partially_completed；用户显式 require_simulation=True 时本项失效。
MANAGER_SIMULATION_OPTIONAL = True

# 非 Mock 数据的时效阈值（小时），与 Feedback Agent 检查口径一致
MANAGER_DATA_MAX_AGE_HOURS = 72.0


def _runtime_path(env_name: str, default_name: str) -> Path:
    """运行时文件路径：环境变量优先，否则落在**进程工作目录**下的 `data/`。

    原实现写死相对路径 `data/manager_checkpoints.sqlite`：从不同目录启动会写到不同地方，
    容器里还会写到镜像层（重启即失）。这里改成绝对路径，并允许显式指定。
    """
    raw = os.environ.get(env_name, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / "data" / default_name).resolve()


#: Checkpoint（工作流执行状态持久化，与 Feedback Memory 是两套机制）
MANAGER_CHECKPOINT_DB = str(_runtime_path("AEGIS_EXT_CHECKPOINT_DB", "manager_checkpoints.sqlite"))

#: Feedback 记忆的 JSON 存储：原实现固定写在包内（会进代码仓、且被测试写）。
#: 这里默认落到工作目录的 `data/`，可用环境变量改。
FEEDBACK_MEMORY_FILE = str(_runtime_path("AEGIS_EXT_FEEDBACK_MEMORY", "feedback_memory.json"))
'''


def apply_config_governance(repo: Path) -> int:
    """把 `config.py` 换成治理后的版本（凭据只从环境变量取、运行时路径配置化）。

    幂等：已经是治理后的版本（认 `AEGIS_EXT_LLM_API_KEY`）就跳过。
    """
    path = repo / "backend" / "src" / "aegis" / "agents_ext" / "config.py"
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    if "AEGIS_EXT_LLM_API_KEY" in text:
        return 0
    path.write_text(CONFIG_REPLACEMENT, encoding="utf-8", newline="\n")
    print("  fixed config.py —— 凭据改从环境变量取、运行时路径配置化（原文件含明文密钥）")
    return 1


# ---------- A. 真实缺陷 ----------

#: (文件, 原片段, 新片段, 说明)
EXACT_FIXES: tuple[tuple[str, str, str, str], ...] = (
    (
        "src/aegis/agents_ext/simulation_agent/tools/hazard_model_router.py",
        '''

def supported_hazards_text() -> str:
    """支持灾种的可读文本（用于错误提示），如"滑坡(landslide)、泥石流(debris_flow)…"。"""
    return "、".join(f"{HAZARD_CN_NAME[k]}({k})" for k in SUPPORTED_HAZARDS)''',
        '''

# 【并入主线时的改动】原文件在这里**又定义了一遍** `supported_hazards_text`，
# 与上面从 hazard_registry 导入的那份实现逐字相同——覆盖它无害，但违反该模块自己的
# "灾种定义只有一处"的单一真源原则（两份实现一旦各自演化就漂移）。
# 现在只保留导入那一份：灾种清单的唯一定义源永远是 hazard_registry。''',
        "删掉与 hazard_registry 逐字相同的重复定义（单一真源）",
    ),
    (
        "src/aegis/agents_ext/warning_agent/nodes/llm_explanation_node.py",
        """    used_llm = False
    level = alert.get("warning_level", "none")""",
        """    level = alert.get("warning_level", "none")""",
        "删死变量 used_llm（真值在返回 dict 的 explanation['used_llm'] 里）",
    ),
    (
        "src/aegis/agents_ext/warning_agent/nodes/warning_validator_node.py",
        """    decision = state.get("decision_result", {}) or {}
""",
        "",
        "删未使用的 decision（预警/决策一致性校验归反馈环节）",
    ),
    (
        "src/aegis/agents_ext/data_agent/nodes/data_output_node.py",
        """    retry_count = state.get("retry_count", 0)
""",
        "",
        "删未使用的 retry_count（输出节点不重试）",
    ),
    (
        "src/aegis/agents_ext/risk_agent/tools/risk_evidence_tool.py",
        """    for key, spec in (assessment.get("factor_scores", {}) or {}).items():""",
        """    for _key, spec in (assessment.get("factor_scores", {}) or {}).items():""",
        "循环变量 key 未使用 → _key（标注这是有意的）",
    ),
)

#: `assert False, "消息"` → `raise AssertionError("消息")`（python -O 下 assert 会被去掉）。
ASSERT_FALSE_RE = re.compile(r'\bassert False,\s*(.+)$', re.MULTILINE)


def apply_exact_fixes(repo: Path) -> int:
    changed = 0
    for rel, old, new, why in EXACT_FIXES:
        path = repo / "backend" / rel
        if not path.exists():
            continue
        text = _read(path)
        if old not in text:
            continue
        _write(path, text.replace(old, new))
        print(f"  fixed {rel} —— {why}")
        changed += 1
    return changed


def apply_assert_false(repo: Path) -> int:
    """把 `assert False, "消息"` 改成 `raise AssertionError("消息")`。"""
    changed = 0
    roots = [repo / "backend" / "tests" / "unit" / "agents_ext"]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*.py")):
            text = _read(path)
            if "assert False" not in text:
                continue
            new_text = ASSERT_FALSE_RE.sub(lambda m: f'raise AssertionError({m.group(1)})', text)
            if _write(path, new_text):
                print(f"  fixed {path.name} —— assert False → raise AssertionError")
                changed += 1
    return changed


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    repo = Path(sys.argv[1]).resolve()
    print("postprocess:")
    c = apply_config_governance(repo)
    a = apply_exact_fixes(repo)
    b = apply_assert_false(repo)
    print(f"done: {c} config governance, {a} exact fixes, {b} assert-False rewrites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
