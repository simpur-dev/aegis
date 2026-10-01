"""配置面上的旋钮必须真的拧得动：`Settings` 里每个字段都要被生产代码读到。

这不是洁癖，是同一类事故的另一半。`extra="ignore"` 会让 compose 里写错的键被静默丢掉
（`test_config_env_surface.py` 已经守住那半边）；而"键存在、没人读"更隐蔽——运维改了值、
重启了服务、健康检查全绿，实际什么都没变。今晚实测抓到三个这样的键：
`simulator_enabled`（.env.example 明明白白写着 `AEGIS_SIMULATOR_ENABLED`，容器却只看形参）、
`weather_api_base_url`（连接器写了但装配层从不构造）、`connector_poll_seconds`（与
`simulator_interval_seconds` 同义重复，两个旋钮管同一件事注定有一个是假的）。

确实要留着但还没接线的，必须进 `RESERVED` 并给理由，而且代码路径必须响亮拒绝——
`delivery_mode=http` 走的是 `NotImplementedError`，不是静默回落 mock 通道。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aegis.config import Settings
from aegis.container import create_container, default_channels

# 声明了但故意未接线：值 → 为什么可以留着（配套必须有响亮拒绝，见下面那条参数化用例）
RESERVED: dict[str, str] = {
    "delivery_http_base_url": "真实短信/北斗网关待现场凭据；delivery_mode=http 显式 NotImplementedError",
}

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "aegis"
ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"
_DOTENV_KEY = re.compile(r"^(AEGIS_[A-Z0-9_]+)=", re.MULTILINE)


def production_text() -> str:
    """装配层与内核的全部源码（config.py 除外：字段定义本身不算"被读到"）。"""
    chunks = [
        path.read_text(encoding="utf-8")
        for path in SOURCE_ROOT.rglob("*.py")
        if path.name != "config.py" and "__pycache__" not in path.parts
    ]
    return "\n".join(chunks)


def settings_field_for(name: str) -> str:
    return name.removeprefix("AEGIS_").lower()


class TestKnobsAreReachable:
    def test_every_field_is_read_by_production_code(self) -> None:
        text = production_text()
        unread = [name for name in Settings.model_fields if name not in text and name not in RESERVED]
        assert not unread, f"配置面上拧不动的旋钮: {unread}"

    def test_source_root_actually_contains_code(self) -> None:
        """路径挪坏会让上面的断言变成"空文本里没有未读字段"——先确认量到的是真源码。"""
        assert (SOURCE_ROOT / "container.py").is_file()
        assert len(production_text()) > 100_000

    def test_documented_env_keys_are_all_wired(self) -> None:
        text = production_text()
        keys = _DOTENV_KEY.findall(ENV_EXAMPLE.read_text(encoding="utf-8"))
        assert len(keys) >= 20
        orphan = [key for key in keys if settings_field_for(key) not in text and settings_field_for(key) not in RESERVED]
        assert not orphan, f".env.example 里承诺了但生产代码不读的键: {orphan}"

    def test_reserved_knobs_are_still_declared(self) -> None:
        """预留清单也会腐烂：字段被删了或被接线了，都要在这里显式处理而不是留下幽灵条目。"""
        text = production_text()
        wired = [name for name in RESERVED if name in text]
        assert not wired, f"已接线的旋钮仍挂在 RESERVED 上: {wired}"
        assert all(name in Settings.model_fields for name in RESERVED)


class TestReservedKnobsRefuseLoudly:
    def test_http_delivery_mode_raises_instead_of_falling_back_to_mock(self) -> None:
        settings = Settings(env="test", bus_backend="memory", delivery_mode="http", delivery_http_base_url="https://gw.internal")
        with pytest.raises(NotImplementedError):
            default_channels(settings)

    def test_mock_delivery_mode_is_the_wired_default(self) -> None:
        settings = Settings(env="test", bus_backend="memory", delivery_mode="mock")
        channels = default_channels(settings)
        assert {c.value for c in channels} == {"sms", "beidou", "broadcast", "wechat"}


class TestWiredKnobsChangeBehavior:
    """接线不是"代码里出现了这个字段"，而是改值之后平台真的不一样。"""

    def test_simulator_switch_controls_the_generator_source(self) -> None:
        off = create_container(Settings(env="test", bus_backend="memory", simulator_enabled=False))
        on = create_container(Settings(env="test", bus_backend="memory", simulator_enabled=True))
        assert off.simulator is None
        assert [source.name for source in off.ingest.sources] == []
        assert on.simulator is not None
        assert [source.name for source in on.ingest.sources] == [on.simulator.name]

    def test_explicit_argument_still_overrides_the_knob(self) -> None:
        """显式形参优先：演练与测试要能造出"配置说关掉但我要一个模拟器"的容器。"""
        ctn = create_container(Settings(env="test", bus_backend="memory", simulator_enabled=False), with_simulator=True)
        assert ctn.simulator is not None

    def test_weather_switch_controls_the_pull_source(self) -> None:
        off = create_container(Settings(env="test", bus_backend="memory", simulator_enabled=False))
        on = create_container(
            Settings(env="test", bus_backend="memory", simulator_enabled=False, weather_api_base_url="https://weather.example.internal"),
            weather_client=object(),
        )
        assert off.weather is None
        assert [source.name for source in on.ingest.sources] == ["weather_api"]
