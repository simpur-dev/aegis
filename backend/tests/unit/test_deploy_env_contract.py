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
