"""一条命令起两端这件事的契约对账（README ↔ 根 package.json ↔ 两端默认端口）。

`npm run dev` 是**跨三个文件**拼出来的：仓库根清单里的脚本、后端 `Settings.http_port` 默认值、
前端 `vite.config.ts` 的代理目标默认值。三处各自都对、拼起来打不开的情况长这样：
后端默认端口改成 8010 而 vite 还指向 8000 —— 前端页面全 404，但两端单看都是绿的。
这类漂移人眼扫不出来，只能对账（同 `test_deploy_env_contract.py` 的立场）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from aegis.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
ROOT_MANIFEST = REPO_ROOT / "package.json"
VITE_CONFIG = REPO_ROOT / "frontend" / "vite.config.ts"
README = REPO_ROOT / "README.md"


def manifest() -> dict[str, object]:
    return json.loads(ROOT_MANIFEST.read_text(encoding="utf-8"))


def scripts() -> dict[str, str]:
    value = manifest().get("scripts", {})
    assert isinstance(value, dict), "根 package.json 的 scripts 不是对象"
    return value


class TestRootDevEntrypoint:
    def test_dev_script_exists_and_is_the_one_readme_tells_people_to_run(self) -> None:
        assert "dev" in scripts(), "根 package.json 没有 dev 脚本，README 那条 `npm run dev` 是空头支票"
        readme = README.read_text(encoding="utf-8")
        assert "npm run dev" in readme, "README 的快速开始不再提这条命令，就要同步改这里"

    def test_the_orchestrator_is_a_declared_dependency(self) -> None:
        """`dev` 里点了 concurrently，它就必须写在依赖里。

        否则新克隆的机器上 `npm run dev` 报的是 "concurrently 不是内部或外部命令"，
        而这条失败只在**没跑过 npm install 的人**身上出现——门禁要替他先撞一次。
        """
        dev_script = scripts()["dev"]
        assert "concurrently" in dev_script, "dev 脚本没在用 concurrently，那这个依赖声明该一起删"
        deps = {**(manifest().get("devDependencies") or {})}
        assert "concurrently" in deps, f"dev 用了 concurrently 但没声明为 devDependency：{sorted(deps)}"
        assert "dependencies" not in manifest() or "concurrently" not in (manifest().get("dependencies") or {}), (
            "开发编排工具混进了运行时依赖"
        )

    def test_both_legs_point_at_directories_that_exist(self) -> None:
        missing: list[str] = []
        for name in ("dev:backend", "dev:frontend"):
            command = scripts().get(name)
            assert command, f"根 package.json 缺脚本 {name}"
            for target in re.findall(r"cd\s+([A-Za-z0-9_./-]+)", command):
                if not (REPO_ROOT / target).is_dir():
                    missing.append(f"{name} -> {target}")
        assert not missing, f"脚本里 cd 的目录不存在：{missing}"

    def test_backend_leg_runs_the_module_the_api_layer_defines(self) -> None:
        assert "aegis.main" in scripts()["dev:backend"], "后端入口换了模块名，这条 dev 脚本与 README 都要跟着改"

    def test_default_ports_on_both_ends_agree(self) -> None:
        """后端**声明的默认**监听端口 == vite 代理默认目标端口。

        两端各写一个默认值，改一处就会得到"后端起来了、前端全 404"，
        而且两边单独看都正常——所以这条断言比它看起来重要。
        取 `model_fields[...].default` 而不是实例值：`.env.example` 里就有 `AEGIS_HTTP_PORT=8000`，
        按 README 复制一份后有人改成 8010 是正当的本机配置，那时该红的是"两个默认值不一致"，
        不是"他的本地覆盖没顺着 vite 的默认走"。
        """
        backend_port = Settings.model_fields["http_port"].default
        vite_text = VITE_CONFIG.read_text(encoding="utf-8")
        match = re.search(r"AEGIS_API_TARGET\s*\?\?\s*'http://127\.0\.0\.1:(\d+)'", vite_text)
        assert match, f"vite.config.ts 里代理目标的写法变了，对账逻辑要跟着改：{vite_text.splitlines()[3]!r}"
        assert int(match.group(1)) == backend_port, (
            f"后端默认 {backend_port} 而 vite 默认指向 {match.group(1)}：`npm run dev` 会两端都绿、页面全 404"
        )

    def test_frontend_leg_goes_through_its_own_dev_script(self) -> None:
        """必须走 `npm run dev` 而不是直接 npx vite：`predev` 会准备 Cesium 静态资产。"""
        assert re.search(r"npm\s+run\s+dev", scripts()["dev:frontend"]), "绕过 predev 会让一张图在 dev 下缺资产"
        frontend_manifest = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        assert "predev" in frontend_manifest["scripts"], "前端 predev 被删了，这条链路的假设要重看"
