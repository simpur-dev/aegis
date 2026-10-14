"""入库产物与生成器脚本的行尾口径：产物字节不能跟着烘焙机走。

起因（2026-10-13 实算）：`git ls-files --eol` 报 `backend/baselines/quality_baseline.json` 是
`i/lf w/crlf` —— 入库那档是 LF，而本机用 `write_text(...)` 默认文本模式重跑一遍就得到 CRLF。
`.gitattributes` 的 `eol=lf` 把这个差异抹平在 git 视角里，所以**没有任何一档门禁看得见它**：
"生成器能重现入库产物"这句在字节层面从未成立。离线瓦的 `layer.json` 是同一处毛病的第一例。
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BACKEND = REPO / "backend"


def generated_sources() -> list[Path]:
    """所有会往仓库里写产物的脚本（生成器与报告器），不含测试自身。"""
    return [*sorted((BACKEND / "scripts").glob("*.py")), REPO / "scripts" / "build_offline_tiles.py"]


def test_入库产物里没有回车符() -> None:
    """逐字节读回入库的那几份生成产物：出现 \\r 就说明它是被某台 Windows 机器写出来的。"""
    artifacts = [
        BACKEND / "baselines" / "quality_baseline.json",
        REPO / "frontend" / "public" / "terrain" / "layer.json",
    ]
    for path in artifacts:
        assert path.is_file(), f"产物不在位：{path}"
        raw = path.read_bytes()
        assert b"\r" not in raw, f"{path.relative_to(REPO)} 里出现 CR：入库档必须是 LF，否则换台机器重烘就整体不同"


def test_生成器写文件必须显式钉死换行() -> None:
    """源级门禁：任何 `write_text(...)` 不带 `newline=` 都算违规。

    用 `ast` 而不是正则：正则在第一个右括号就停，`write_text(json.dumps(...) + "\\n", encoding=...)`
    会被切成半句，于是把**已经写对的代码**报成违规——探测器自己错了却去判产物有问题，
    本仓已经栽过两次（校验器先被校验）。
    为什么不在测试里"跑一遍看产物"：脚本各有前置条件（基线、台账、模型），要全部跑齐才覆盖得到
    所有写点；而这条缺陷的成因就在源码形状上，读源比凑运行条件更直接。
    """
    offenders: list[str] = []
    for path in generated_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "write_text"):
                continue
            if not any(keyword.arg == "newline" for keyword in node.keywords):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert not offenders, f"写产物却没钉换行（Windows 上会产出 CRLF）：{offenders}"


def test_quality_gate_重写的基线档仍是_lf(tmp_path: Path) -> None:
    """真跑一次写盘路径：`--write-baseline` 落到临时目录，字节里不能有 \\r。"""
    target = tmp_path / "quality_baseline.json"
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.quality_gate", "--write-baseline", str(target)],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"quality_gate 退出码 {proc.returncode}\n{proc.stdout[-600:]}\n{proc.stderr[-600:]}"
    assert target.is_file(), "--write-baseline 没写出档"
    assert b"\r" not in target.read_bytes(), "重写出来的基线档带 CR：newline 参数没生效"


def test_gate_作业装了上游读取器源码供守卫使用() -> None:
    """两条"上游解析顺序漂移"守卫读的是 `frontend/node_modules`，而它不在版本控制里。

    gate job 若不跑 npm ci，这两条在 CI 上永远 skip——本机装了就会跑，两边都不能算测过。
    这里按部署面判定（读 ci.yml），与 MQTT/MCP 那两条同形。
    """
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    gate_job = ci.split("\n  frontend:")[0]
    # 针必须钉到具体那一行：早先写成查 "npm ci"，我的反例占位里恰好含这三个字，
    # 于是"摘掉 npm ci"的变异照样绿——那是坏反例不是好守卫。
    assert 'npm --prefix "$GITHUB_WORKSPACE/frontend" ci' in gate_job, (
        "gate job 没装前端源码依赖：Cesium/pmtiles 的漂移守卫在 CI 上永远跳过"
    )
    assert "node-version-file: .nvmrc" in ci, "node 版本没跟着 .nvmrc 走：CI 验的是没人用的那台"
