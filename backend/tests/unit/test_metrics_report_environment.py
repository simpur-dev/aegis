"""批量口径工具的"测量环境"出口：报表必须自己说清这数是在什么上量出来的。

`metrics_report` 以前把 `bus_backend="memory"` 写死在 `run()` 里，于是它只能量"内存总线 + 内存
视图 + 降级轨"这一种世界——而 §6 要的出口数字属于真栈（NATS + PostgreSQL + real 运行时）。
两种世界的 JSON 长得一模一样，读的人分不出来，这是最省事也最危险的假话。
所以这里钉两件事：① 缺凭据时【当场炸】，不静默退回内存；② 出口布尔只在全真栈 + real + LLM 已配
时才为真——真内环跑在没有 key 的降级轨上同样不算出口数字。
"""

from __future__ import annotations

import pytest
from scripts.metrics_report import measurement_environment, resolve_settings


def test_默认口径是内存世界() -> None:
    settings = resolve_settings(bus_backend="memory", store_backend="memory", agent_runtime="mock", seed=1)
    assert (settings.bus_backend, settings.store_backend, settings.agent_runtime) == ("memory", "memory", "mock")
    assert settings.delivery_mode == "mock", "交付腿默认 mock：出口数字里凡是真发送的都得显式改 http"


def test_真总线缺地址就当场炸而不是退回内存() -> None:
    with pytest.raises(ValueError, match="AEGIS_NATS_URL"):
        resolve_settings(bus_backend="nats", store_backend="memory", agent_runtime="real", seed=1, environ={})
    settings = resolve_settings(
        bus_backend="nats",
        store_backend="memory",
        agent_runtime="real",
        seed=1,
        environ={"AEGIS_NATS_URL": "nats://127.0.0.1:4222"},
    )
    assert settings.bus_backend == "nats" and settings.nats_url == "nats://127.0.0.1:4222"


def test_真存储缺dsn就当场炸() -> None:
    with pytest.raises(ValueError, match="AEGIS_PG_DSN"):
        resolve_settings(bus_backend="memory", store_backend="postgres", agent_runtime="mock", seed=1, environ={})
    settings = resolve_settings(
        bus_backend="memory",
        store_backend="postgres",
        agent_runtime="mock",
        seed=1,
        environ={"AEGIS_PG_DSN": "postgresql://aegis:pw@127.0.0.1:5432/aegis"},
    )
    assert settings.store_backend == "postgres" and settings.pg_dsn.startswith("postgresql://")


def test_内存世界明说不可作出口数字() -> None:
    settings = resolve_settings(bus_backend="memory", store_backend="memory", agent_runtime="mock", seed=1)
    block = measurement_environment(settings, llm_configured=False, real_agents=0, mock_agents=4)
    assert block["可作出口数字"] is False
    assert "【不可】" in block["口径提醒"]
    assert block["在册智能体"] == {"真内环": 0, "mock 替身": 4}


def test_真内环跑在没有key的降级轨上也不算出口数字() -> None:
    """这是最容易自欺的一格：五副真智能体确实在总线上应答了，但没有模型调用，时延不代表真模型。"""
    settings = resolve_settings(
        bus_backend="nats",
        store_backend="postgres",
        agent_runtime="real",
        seed=1,
        environ={"AEGIS_NATS_URL": "nats://x:4222", "AEGIS_PG_DSN": "postgresql://y"},
    )
    block = measurement_environment(settings, llm_configured=False, real_agents=5, mock_agents=0)
    assert block["可作出口数字"] is False
    assert "未配" in block["LLM 网关"]


def test_全真栈且LLM已配才亮绿灯() -> None:
    settings = resolve_settings(
        bus_backend="nats",
        store_backend="postgres",
        agent_runtime="real",
        seed=1,
        environ={"AEGIS_NATS_URL": "nats://x:4222", "AEGIS_PG_DSN": "postgresql://y"},
    )
    block = measurement_environment(settings, llm_configured=True, real_agents=5, mock_agents=0)
    assert block["可作出口数字"] is True
    assert "可按 §6" in block["口径提醒"]
    assert (block["总线"], block["存储"], block["智能体运行时"]) == ("nats", "postgres", "real")
