"""compose 与仓库文档之间关于"哪份 .env"的契约。

真实事故形状：`.env.example` 在仓库根、README 也让人 cp 到仓库根，
但 `docker compose -f deploy/docker-compose.yml ...` 的变量插值默认只去 **compose 文件所在目录**
（deploy/）找 .env——照文档做完的人仍会在 `POSTGRES_PASSWORD` 这类必填项上直接被拒。
本机实测：不带 `--env-file .env` 时 `config` 就报 5 个必填变量缺失，带上才解析干净。
这类"两条指令各自都对、拼起来跑不起来"的问题只能靠对账钉住，人眼扫不出来。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]  # backend/tests/unit/... → 仓库根
COMPOSE = REPO_ROOT / "deploy" / "docker-compose.yml"
ENV_TEMPLATE = REPO_ROOT / ".env.example"
README = REPO_ROOT / "README.md"

REQUIRED_PATTERN = re.compile(r"\$\{([A-Z0-9_]+):\?[^}]*\}")
ANY_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-[^}]*)?\}")


def compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def template_keys() -> set[str]:
    return {
        match.group(1)
        for line in ENV_TEMPLATE.read_text(encoding="utf-8").splitlines()
        if (match := re.match(r"^([A-Z0-9_]+)=", line.strip()))
    }


def compose_command_lines() -> list[str]:
    """README 里真正给人抄的那几行 compose 命令（代码块内）。"""
    lines = []
    for raw in README.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.startswith("docker compose") and "deploy/docker-compose.yml" in stripped:
            lines.append(stripped)
    return lines


class TestComposeEnvContract:
    def test_required_variables_are_all_in_the_template(self) -> None:
        keys = template_keys()
        missing = sorted(set(REQUIRED_PATTERN.findall(compose_text())) - keys)
        assert not missing, f"compose 里这些必填变量在 .env.example 没有出处：{missing}"

    def test_every_referenced_variable_has_a_template_entry_or_a_default(self) -> None:
        keys = template_keys()
        offenders: list[str] = []
        for line in compose_text().splitlines():
            if "${" not in line:
                continue
            defaulted = ":-" in line
            for name in ANY_VAR_PATTERN.findall(line):
                if name not in keys and not defaulted:
                    offenders.append(f"{name} @ {line.strip()[:80]}")
        assert not offenders, f"引用了模板里没有、也没写默认值的变量：{offenders}"

    def test_documented_compose_commands_pin_the_env_file(self) -> None:
        commands = compose_command_lines()
        assert commands, "README 里得留着可直接抄写的 compose 命令"
        without = [line for line in commands if "--env-file" not in line]
        assert not without, f"这些 compose 命令没带 --env-file，插值会去 deploy/ 找 .env 而失败：{without}"

    def test_template_is_told_to_land_in_the_repo_root(self) -> None:
        readme = README.read_text(encoding="utf-8")
        assert "cp .env.example .env" in readme, "README 必须写明 compose 用的是仓库根那份 .env"
        assert "cp .env.example backend/.env" in readme, "本机进程用的 backend/.env 那条也还在（两者用途不同）"

    def test_missing_variable_messages_name_the_file(self) -> None:
        ambiguous = [line.strip() for line in compose_text().splitlines() if ":?" in line and "需在 .env" in line]
        assert not ambiguous, f"报错只说「.env」不说哪一份，运维会去建错的文件：{ambiguous}"

    def test_container_env_file_and_interpolation_point_at_the_same_file(self) -> None:
        """compose 的 env_file 相对项目目录（deploy/）解析，`../.env` 才是仓库根那一份。"""
        assert re.search(r"^\s*-\s*\.\./\.env\s*$", compose_text(), re.MULTILINE), (
            "backend 容器的 env_file 应指向仓库根 .env（与 --env-file .env 同一份），改成别的路径要连 README 一起改"
        )

    def test_ci_validates_the_deploy_surface(self) -> None:
        """文档命令与告警配置都必须在 CI 里被真跑一次，否则这里的对账只守住了一半。

        踩过的形状：`alerts.yml` 单独过过 promtool，但 `prometheus.yml` 里既没 rule_files
        也没 alerting 段——"规则文件存在"与"告警链路在跑"是两件事，构建必须验后者。
        """
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "cp .env.example .env" in ci, "CI 得按文档的路径造 .env，再校验 compose"
        assert re.search(r"docker compose --env-file \.env -f deploy/docker-compose\.yml[\s\S]{0,120}config --quiet", ci), (
            "CI 必须解析带三个 profile 的 compose 文件"
        )
        assert "promtool" in ci and "check rules" in ci, "告警规则要由 promtool 在 CI 里校验"
        assert "check config" in ci, "采集配置（rule_files / alerting 段）要在 CI 里过 promtool"
        assert "amtool" in ci and "check-config" in ci, "分发与抑制配置要在 CI 里过 amtool"


HEALTH_URL_PORT = re.compile(r"http://127\.0\.0\.1:(\d+)")


def compose_service_blocks() -> dict[str, str]:
    """按两空格缩进把 `services:` 下的每个服务块切出来（不引 yaml 依赖，保持本文件的纯文本对账口径）。"""
    lines = compose_text().splitlines()
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        named = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if named:
            current = named.group(1)
            blocks[current] = []
            continue
        if current is not None and re.match(r"^[A-Za-z]", line):
            current = None  # 到了 volumes:/name: 这类顶层键，服务块结束
        if current is not None:
            blocks[current].append(line)
    return {name: "\n".join(body) for name, body in blocks.items()}


class TestHealthcheckPortIsActuallyOpened:
    """healthcheck 打的端口必须由该服务自己显式开启，否则"服务不健康"看起来像镜像坏了。

    真事故形状（2026-10-02 实测）：nats 的 healthcheck 打 `127.0.0.1:8222/healthz`，
    而 nats-server **默认不开监控端口**（要 `-m 8222`）。在容器里 wget 那个地址是
    connection refused ⇒ healthcheck 永远失败 ⇒ 用 `depends_on: service_healthy` 的
    backend 永远起不来。`docker compose config` 查不出这种错，因为它只看语法。
    """

    def test_every_localhost_healthcheck_port_is_enabled_in_the_command(self) -> None:
        offenders: list[str] = []
        for name, body in compose_service_blocks().items():
            ports = set(HEALTH_URL_PORT.findall(body))
            if not ports:
                continue
            command = " ".join(line for line in body.splitlines() if "command:" in line)
            if not command:
                continue  # 没有 command 的服务靠镜像默认监听，静态证不了，跳过而不是判过
            for port in sorted(ports):
                if port not in command:
                    offenders.append(f"{name}: healthcheck 打 :{port}，但 command 里没开启它")
        assert offenders == []

    def test_nats_healthcheck_port_is_the_one_its_command_opens(self) -> None:
        """nats 单独钉一条：它是 backend 的启动前置条件，这条断了整条 app profile 就起不来。"""
        body = compose_service_blocks()["nats"]
        assert '"-m", "8222"' in body, "nats 的监控端口要在 command 里显式开，healthcheck 才打得到"
        backend = compose_service_blocks()["backend"]
        assert "nats:" in backend and "condition: service_healthy" in backend, "backend 以 nats 健康为启动前置条件"
