"""配置面一致性测试：部署文件里写的每个 AEGIS_* 都必须对应一个真实的 Settings 字段。

这条断言存在的理由是一次真实事故形态：compose 里写 `AEGIS_DB_URL`，而 `Settings` 里的旋钮
叫 `pg_dsn`；因为 `extra="ignore"`，错名字段被静默丢弃，平台带着内存视图启动、健康检查全绿，
运维却以为数据在落库。这类分歧只能靠机器守，不能靠人读。
"""

from __future__ import annotations

import re
from pathlib import Path

from aegis.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE = REPO_ROOT / "deploy" / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# compose 的 service 环境变量固定缩进 6 空格；.env.example 是行首 KEY=
_COMPOSE_KEY = re.compile(r"^ {6}(AEGIS_[A-Z0-9_]+):", re.MULTILINE)
_DOTENV_KEY = re.compile(r"^(AEGIS_[A-Z0-9_]+)=", re.MULTILINE)
_ANY_DOTENV_KEY = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.MULTILINE)
# ${VAR} 与 ${VAR:?说明} 没有默认值，缺了 compose 直接失败；${VAR:-默认} 有默认值，可以留空
_COMPOSE_INTERPOLATION = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::([^}]*))?\}")


def settings_field_for(name: str) -> str | None:
    """AEGIS_PG_DSN -> pg_dsn：只认 env_prefix 直译，别名与校验器不做映射。"""
    field = name.removeprefix("AEGIS_").lower()
    return field if field in Settings.model_fields else None


def compose_keys() -> list[str]:
    return _COMPOSE_KEY.findall(COMPOSE.read_text(encoding="utf-8"))


def dotenv_keys() -> list[str]:
    return _DOTENV_KEY.findall(ENV_EXAMPLE.read_text(encoding="utf-8"))


def mandatory_interpolations() -> set[str]:
    """compose 里"必须提供、否则起不来"的变量：`${VAR}` 与 `${VAR:?说明}` 都没有默认值。"""
    text = COMPOSE.read_text(encoding="utf-8")
    return {name for name, modifier in _COMPOSE_INTERPOLATION.findall(text) if modifier is None or modifier.startswith("?")}


def all_dotenv_keys() -> set[str]:
    return set(_ANY_DOTENV_KEY.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


class TestDeploymentEnvKeys:
    def test_the_files_under_test_actually_exist(self) -> None:
        """先确认文件在：路径挪动会让上面的正则空匹配，而"零变量"看起来一样是绿的。"""
        assert COMPOSE.is_file() and ENV_EXAMPLE.is_file()
        assert len(compose_keys()) >= 6
        assert len(dotenv_keys()) >= 20

    def test_compose_sets_only_real_settings_knobs(self) -> None:
        unknown = [key for key in compose_keys() if settings_field_for(key) is None]
        assert not unknown, f"docker-compose 注入了不存在的配置项（会被静默忽略）: {unknown}"

    def test_env_example_documents_only_real_settings_knobs(self) -> None:
        unknown = [key for key in dotenv_keys() if settings_field_for(key) is None]
        assert not unknown, f".env.example 里写了不存在的配置项: {unknown}"

    def test_every_mandatory_compose_variable_is_documented(self) -> None:
        """compose 用 `${VAR:?…}` 强制要求的变量，照抄 .env.example 起的环境必须一个不缺。

        真实教训：`EMQX_DASHBOARD_PASSWORD` 只在 compose 里要求、示例里没有，`docker compose config`
        当场解析失败——这种错误发生在部署阶段，单测不盯就只剩运维在客户现场碰。
        """
        mandatory = mandatory_interpolations()
        assert len(mandatory) >= 4, f"没抓到必需变量，正则可能已随 compose 结构失效: {mandatory}"
        missing = sorted(mandatory - all_dotenv_keys())
        assert not missing, f".env.example 缺少 compose 必需变量: {missing}"

    def test_no_sqlalchemy_era_dsn_knob_survives(self) -> None:
        """`db_url` 已随 Postgres 接线一并删除：它默认指向 sqlite，而持久层只认 PostgreSQL。"""
        text = f"{COMPOSE.read_text(encoding='utf-8')}\n{ENV_EXAMPLE.read_text(encoding='utf-8')}"
        assert "AEGIS_DB_URL" not in text
        assert "db_url" not in Settings.model_fields

    def test_production_legs_are_selected_explicitly(self) -> None:
        """三条可选腿（存储/图谱/检索）在 compose 里必须显式选上，否则生产静默降级。"""
        keys = set(compose_keys())
        for required in ("AEGIS_STORE_BACKEND", "AEGIS_PG_DSN", "AEGIS_KNOWLEDGE_GRAPHITI_URI", "AEGIS_RETRIEVAL_ENABLED"):
            assert required in keys, f"compose 缺少 {required}"

    def test_image_installs_every_platform_side_extra(self) -> None:
        """镜像缺一个 extra，对应腿在生产里就只是 /api/v1/integrations 上的一行降级。"""
        dockerfile = (REPO_ROOT / "deploy" / "Dockerfile").read_text(encoding="utf-8")
        installed = set(re.findall(r"--extra ([a-z]+)", dockerfile))
        pyproject = (REPO_ROOT / "backend" / "pyproject.toml").read_text(encoding="utf-8")
        declared = set(re.findall(r"^([a-z]+) = \[", pyproject, re.MULTILINE))
        server_side = {"postgres", "iot", "analytics", "graph", "retrieval"}
        assert server_side <= installed, f"Dockerfile 未安装: {sorted(server_side - installed)}"
        # 边缘与压测用的 extra 不该进平台镜像：它们会把 1GB 级依赖带到服务端
        assert not ({"edge", "perf", "dev", "objectstore"} & installed)
        assert installed <= declared, f"Dockerfile 引用了 pyproject 里不存在的 extra: {sorted(installed - declared)}"

    def test_ci_syncs_the_same_extras_it_tests(self) -> None:
        """CI 只装 dev 会让可选子系统的用例静默跳过，绿色 CI 等于什么都没测。"""
        installed = set(re.findall(r"--extra ([a-z]+)", CI.read_text(encoding="utf-8")))
        assert {"postgres", "analytics", "retrieval", "graph", "edge", "perf"} <= installed
