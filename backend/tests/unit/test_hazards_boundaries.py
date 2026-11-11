"""智能体层的**边界守卫**——数据边界的**唯一真源**（一份规则，两档作用域）。

为什么要有这一组：智能体是"带着自己的依赖进来的第二套实现"，最容易被后来的人改坏的
三条边界是——① 它开始直连数据库；② 它绕过契约壳直接被平台调用；③ 它变成平台的硬依赖
（没装可选 extra 就起不来）。这三条一旦破口，"两套并存、按需选择"的设计就悄悄变成了
"两套耦合、必须都装"。

【本文件是唯一的数据边界守卫】此前 Gate6 的静态扫描散在四处（两份 conformance、
本文件、architecture_rules），各自禁令集合宽严不一——audit 实锤"三方各有一条别人
没有的牙"。统一原则：**取更严的那方，集中在这里**。两份 conformance 的 Gate6 只保留
运行时实例形状断言（静态扫描不再重复）；`test_architecture_rules.py` 管的是分层
依赖方向（另一件事，不属于数据边界）。

【两档作用域】
- **全域**（`src/aegis/agents/**`）：不得直连数据库/存储层、不得改 `sys.path`、不得留
  明文凭据。平台自建五段与灾种六体**同一条**纪律。
- **加严**（`src/aegis/agents/hazards/**`）：白名单制（**默认拒绝**）——对 `aegis.*`
  的依赖只允许契约面（domain / bus / errors / 契约壳 / 能力矩阵）与它自己的包。
  黑名单式"禁 services/pipeline/api/workflow"永远在追新名词，白名单让"伸手进平台
  内部"结构性不可能；它是"总线上的一条独立服务"，这条边界就是它的定义。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
AGENTS = BACKEND / "src" / "aegis" / "agents"
PACKAGE = AGENTS / "hazards"

#: 与平台侧 Gate6 同一组禁令（`test_agent_conformance.py::TestGate6DataBoundary`）：
#: 智能体侧不得直连数据库/存储层——取数一律经平台数据 API 或总线。
FORBIDDEN_IMPORTS = {"sqlalchemy", "asyncpg", "neo4j", "redis", "minio", "psycopg", "psycopg2"}
FORBIDDEN_PREFIXES = ("aegis.storage", "aegis.persistence")

#: 灾种模块允许碰的平台部分：全是**契约面**，不是内部实现。
#: `aegis.agents.agentkit` 是契约壳（`AgentKit` 基类），`aegis.agents.capabilities` 是
#: 能力矩阵——两者都是"总线上怎么说话"的定义；平台自建本体的内部模块（perceive /
#: assess / plan / execute / feedback）不在此列。
HAZARDS_ALLOWED = (
    "aegis.agents.agentkit",
    "aegis.agents.capabilities",
    # 共享内核（熔断 / 提示词注册表 / 调用台账 / 调用元数据）：L3 智能体层自己的契约面，
    # 与 agentkit、capabilities 同级。放在 L4 的 services/llm 下会逼灾种包做 L3→L4 倒挂依赖。
    "aegis.agents.core",
    "aegis.agents.hazards",
    "aegis.bus",
    "aegis.domain",
    "aegis.errors",
)


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def test_模块存在且有内容() -> None:
    files = _python_files(PACKAGE)
    assert len(files) > 50, f"灾种模块只有 {len(files)} 个文件，搬入是否完整？"
    assert (BACKEND / "src" / "aegis" / "agents" / "factory.py").exists(), "装配入口必须在 agents/factory.py"
    assert (PACKAGE / "risk_agent" / "agent.py").exists(), "因子引擎本体必须直接继承 AgentKit"
    assert (PACKAGE / "contracts.py").exists(), "灾种能力的请求/响应契约必须有唯一真源"


@pytest.mark.parametrize("path", _python_files(AGENTS), ids=lambda p: str(p.relative_to(AGENTS)))
def test_不直连数据库与存储层(path: Path) -> None:
    """智能体与平台自建内环同一条禁令：取数经平台数据 API 或总线，不直连库。"""
    for module in _imported_modules(path):
        root = module.split(".")[0]
        assert root not in FORBIDDEN_IMPORTS, f"{path.name} 引入了数据库驱动：{module}"
        for prefix in FORBIDDEN_PREFIXES:
            assert not module.startswith(prefix), f"{path.name} 引入了存储层：{module}"


@pytest.mark.parametrize("path", _python_files(PACKAGE), ids=lambda p: p.name)
def test_灾种模块的aegis依赖必须在白名单内(path: Path) -> None:
    """契约边界三原则第 1 条：智能体不依赖平台内部实现细节——**白名单制**。

    对 `aegis.*` 的依赖只允许契约面（`HAZARDS_ALLOWED`），名单之外的 aegis 包
    一律违规（默认拒绝）。黑名单式禁令（禁 services/pipeline/api/…）每加一层平台
    包就要追着补一条，漏补即破口；白名单把方向反过来。
    """
    for module in _imported_modules(path):
        if not module.startswith("aegis."):
            continue
        assert module.startswith(HAZARDS_ALLOWED), f"{path.name} 依赖了 {module}——不在灾种模块的白名单内（允许：契约面与它自己的包）"


def test_不写死仓库外路径() -> None:
    """原实现里有 Windows 绝对路径（只在文档字符串里）与相对 CWD 的数据路径。

    相对路径会随启动目录漂移（写到不同地方），绝对路径换台机器就失效。
    这里只查**可执行代码**里的绝对路径与相对数据路径——注释与文档字符串里提到旧路径
    是正常叙述（"原来写死的是 X，现在改成 Y"），不该被当成违规。
    """
    for path in _python_files(AGENTS):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # 文档字符串（模块/函数的第一条表达式常量）不算可执行代码
        docstrings = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            body = getattr(node, "body", [])
            if not body or not isinstance(body[0], ast.Expr) or not isinstance(body[0].value, ast.Constant):
                continue
            if isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                value = node.value
                assert not value.startswith("C:\\"), f"{path.name} 里写死了 Windows 绝对路径：{value}"
                assert not value.startswith("C:/"), f"{path.name} 里写死了 Windows 绝对路径：{value}"
                assert "data/manager_checkpoints.sqlite" not in value, f"{path.name} 用了相对 CWD 的数据路径（会随启动目录漂移）：{value}"


def test_不残留明文密钥() -> None:
    """原仓库把明文 API Key 写进了 config.py 并提交进公开仓库——那把钥匙已经泄露。

    这条门禁钉住"不会再有人往里塞明文凭据"：密钥只能从环境变量来。
    """
    banned = ('ZHIPUAI_API_KEY = "', "sk-", 'api_key="', 'API_KEY = "')
    for path in _python_files(AGENTS):
        text = path.read_text(encoding="utf-8")
        for pattern in banned:
            assert pattern not in text, f"{path.name} 里疑似有硬编码密钥：{pattern}"


def test_配置面只从环境变量取凭据() -> None:
    """`config.py` 的凭据入口必须是环境变量，不能是源码里的字面量。"""
    config = PACKAGE / "config.py"
    assert config.exists(), "灾种模块必须有自己的配置面（凭据来源集中在一处）"
    text = config.read_text(encoding="utf-8")
    assert "os.environ" in text, "config.py 必须从环境变量取凭据"
    # 凭据入口**只有一处**：`HAZARD_LLM_API_KEY = _env("LLM_API_KEY")`，而 `_env` 只读环境变量
    # （新名优先、旧名兜底）。此前它是 `ZHIPUAI_API_KEY = os.environ.get(...)`；改名后断言
    # 得跟着走，否则这条门禁会变成"查一个已经不存在的常量"。
    assert 'HAZARD_LLM_API_KEY = _env("LLM_API_KEY")' in text, "凭据入口必须是环境变量读取（经 _env）"


def test_不修改全局sys_path() -> None:
    """原实现有 39 处 `sys.path.insert` 引导——那会把仓库根塞进 sys.path，
    别的包名（`agents`、`config`、`llm`）就可能被别人 import 到。

    并入主线后这些引导必须全部删除（模块已经是包内绝对导入）。
    """
    offenders = []
    for path in _python_files(AGENTS):
        if "sys.path.insert" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert not offenders, f"这些文件还在改 sys.path：{offenders}"
