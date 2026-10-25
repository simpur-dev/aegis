"""一条命令起两端这件事的契约对账（README ↔ 根 package.json ↔ scripts/dev.mjs ↔ 两端默认端口）。

`npm run dev` 是**跨四个文件**拼出来的：仓库根清单里的三个脚本、`scripts/dev.mjs` 里那份
端口真源、后端 `Settings.http_port` 默认值、前端 `vite.config.ts` 的代理目标默认值。
各处单独都对、拼起来打不开的情况长这样：后端默认端口改成 8010 而 vite 还指向 8000 ——
前端页面全 404，但两端单看都是绿的。这类漂移人眼扫不出来，只能对账
（同 `test_deploy_env_contract.py` 的立场）。

2026-10-24 起编排收进 `scripts/dev.mjs`：`npm run dev` 自己拉起 Docker、起默认档容器
（nats / postgres / neo4j）、停掉容器后端，再以 `AEGIS_STORE_BACKEND=postgres` 起本地后端——
全系统只有一份数据。本地端口固定 8001，与容器发布的 8000 分开（Windows 上 uvicorn 绑
0.0.0.0:8000 会"看着起来了"，而 Docker 已占 127.0.0.1:8000，本地后端将一条请求都收不到）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from aegis.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
ROOT_MANIFEST = REPO_ROOT / "package.json"
DEV_RUNNER = REPO_ROOT / "scripts" / "dev.mjs"
VITE_CONFIG = REPO_ROOT / "frontend" / "vite.config.ts"
README = REPO_ROOT / "README.md"


def manifest() -> dict[str, object]:
    return json.loads(ROOT_MANIFEST.read_text(encoding="utf-8"))


def scripts() -> dict[str, str]:
    value = manifest().get("scripts", {})
    assert isinstance(value, dict), "根 package.json 的 scripts 不是对象"
    return value


def dev_runner() -> str:
    assert DEV_RUNNER.is_file(), "编排脚本 scripts/dev.mjs 不在了，根 package.json 那三条脚本是空头支票"
    return DEV_RUNNER.read_text(encoding="utf-8")


class TestRootDevEntrypoint:
    def test_dev_script_exists_and_is_the_one_readme_tells_people_to_run(self) -> None:
        assert "dev" in scripts(), "根 package.json 没有 dev 脚本，README 那条 `npm run dev` 是空头支票"
        assert "scripts/dev.mjs" in scripts()["dev"], "dev 必须走编排脚本，端口真源只在那一处"
        readme = README.read_text(encoding="utf-8")
        assert "npm run dev" in readme, "README 的快速开始不再提这条命令，就要同步改这里"
        assert "AEGIS_DEV_PORT" in readme, "README 没写端口怎么改，用户撞上占用时只能猜"

    def test_the_orchestrator_is_a_declared_dependency(self) -> None:
        """`dev.mjs` 里 import 了 concurrently，它就必须写在依赖里。

        否则新克隆的机器上 `npm run dev` 报的是 "Cannot find package 'concurrently'"，
        而这条失败只在**没跑过 npm install 的人**身上出现——门禁要替他先撞一次。
        """
        assert "from 'concurrently'" in dev_runner(), "编排脚本没在用 concurrently，那这个依赖声明该一起删"
        deps = {**(manifest().get("devDependencies") or {})}
        assert "concurrently" in deps, f"dev 用了 concurrently 但没声明为 devDependency：{sorted(deps)}"
        assert "dependencies" not in manifest() or "concurrently" not in (manifest().get("dependencies") or {}), (
            "开发编排工具混进了运行时依赖"
        )

    def test_both_legs_are_cut_from_the_same_cloth(self) -> None:
        """两条腿都交给同一个编排脚本，且各自只起自己那一侧。"""
        for name, leg in (("dev:backend", "--only=backend"), ("dev:frontend", "--only=frontend")):
            command = scripts().get(name)
            assert command, f"根 package.json 缺脚本 {name}"
            assert "scripts/dev.mjs" in command and leg in command, f"{name} 没走编排脚本的 {leg} 分支"
        for target in ("backend", "frontend"):
            assert (REPO_ROOT / target).is_dir(), f"{target}/ 不存在，编排脚本里写死的 cwd 就落空了"
            assert f"'{target}'" in dev_runner(), f"编排脚本里找不到 {target} 的工作目录"

    def test_backend_leg_runs_the_module_the_api_layer_defines(self) -> None:
        assert "aegis.main" in dev_runner(), "后端入口换了模块名，编排脚本与 README 都要跟着改"

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

    def test_dev_port_is_single_source_and_kept_off_the_docker_port(self) -> None:
        """本地起两端的端口只能有一个真源，而且不能落在 Docker 栈发布的 8000 上。

        8000 会被容器占着；Windows 允许第二个 socket 再绑同一个端口，于是后端"启动成功"
        却收不到流量。这里把那条约束钉死：真源是 `AEGIS_DEV_PORT`，两条腿都从 `port` 派生。
        """
        text = dev_runner()
        match = re.search(r"AEGIS_DEV_PORT\s*\?\?\s*(\d+)", text)
        assert match, "编排脚本里找不到 AEGIS_DEV_PORT 的默认值，端口真源丢了"
        dev_port = int(match.group(1))
        assert dev_port != Settings.model_fields["http_port"].default, (
            f"本地开发端口 {dev_port} 与后端默认端口撞了：Docker 栈在跑时本地后端会静默收不到请求"
        )
        assert "AEGIS_HTTP_PORT: String(port)" in text, "后端那条腿没从 port 取端口"
        assert "AEGIS_API_TARGET: apiTarget" in text, "前端那条腿没从 port 派生代理目标"
        assert "AEGIS_API_TARGET ?? `http://127.0.0.1:${port}`" in text, "代理目标的默认值不再是 port"
        assert "AEGIS_HTTP_HOST: '127.0.0.1'" in text, "本地后端应只绑回环，别把端口放给局域网"
        readme = README.read_text(encoding="utf-8")
        assert f"127.0.0.1:{dev_port}" in readme, f"README 里的本地 API 地址没跟上 {dev_port} 这个端口"

    def test_dev_bootstraps_the_stack_and_shares_one_postgres(self) -> None:
        """`npm run dev` 必须自己把 Docker 拉起来，并让本地后端连容器那份 postgres。

        这条对的是 2026-10-24 之前的老毛病：本地后端 memory、容器后端 postgres，两套各存一份，
        同一屏预警在两边对不上。一个系统只允许一份数据——编排脚本起容器、停容器后端、
        用同一个库起本地后端，三件事缺一不可。
        """
        text = dev_runner()
        assert "docker-compose.yml" in text, "编排脚本没引用 compose 文件，容器从哪来"
        assert "'--env-file'" in text, "compose 命令必须带 --env-file，否则 5 个必填项当场失败（见 2.3 第 1 条）"
        assert "'up', '-d'" in text, "编排脚本没有 docker compose up -d"
        assert "AEGIS_STORE_BACKEND: 'postgres'" in text, "本地后端又回到 memory 形态了，数据会分成两份"
        assert "AEGIS_PG_DSN: dsn" in text, "本地后端没拿到容器库的 DSN"
        assert "aegis-postgres" in text, "没有等 postgres healthy 就起后端，迁移会撞在连不上库上"
        assert "stopContainer('aegis-backend'" in text, "没停容器后端：两个后端写同一个库会互相踩"
        assert "AEGIS_KNOWLEDGE_GRAPHITI_URI" in text, "知识层没连容器 neo4j，案例数据和容器版不是一份"
        assert "AEGIS_RETRIEVAL_ENABLED: 'true'" in text, "检索腿没跟容器版对齐"
        readme = README.read_text(encoding="utf-8")
        assert "同一份" in readme, "README 没把'一份数据'这件事写清楚"

    def test_frontend_leg_goes_through_its_own_dev_script(self) -> None:
        """必须走 `npm run dev` 而不是直接 npx vite：`predev` 会准备 Cesium 静态资产。"""
        assert re.search(r"command: 'npm\s+run\s+dev'", dev_runner()), "绕过 predev 会让一张图在 dev 下缺资产"
        frontend_manifest = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        assert "predev" in frontend_manifest["scripts"], "前端 predev 被删了，这条链路的假设要重看"
