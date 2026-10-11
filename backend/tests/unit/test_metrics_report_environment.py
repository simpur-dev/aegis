"""批量口径工具的两条硬口径：环境块 + 工作流腿。

一、`metrics_report` 以前把 `bus_backend="memory"` 写死在 `run()` 里，于是它只能量"内存总线 + 内存
视图 + 降级轨"这一种世界——而 §6 要的出口数字属于真栈（NATS + PostgreSQL + real 运行时）。
两种世界的 JSON 长得一模一样，读的人分不出来，这是最省事也最危险的假话。所以钉两件事：
① 缺凭据时【当场炸】，不静默退回内存；② 出口布尔只在全真栈 + real + LLM 已配时才为真——
真内环跑在没有 key 的降级轨上同样不算出口数字。

二、≤2s 那条考核项的定义量测点是 `workflow_schedule_ms`（节点就绪→开始执行）。报表必须有一条
工作流腿真跑实例，否则那一行永远"未测得"，等于考核项没人量。
"""

from __future__ import annotations

import pytest
from scripts.metrics_report import measurement_environment, mixed_registrations, resolve_settings, workflow_leg


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
    assert block["在册智能体"] == {"真内环": 0, "mock 替身": 4, "总线视角总数": 0}


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


async def test_工作流腿把2s考核项的样本跑出来(container) -> None:
    """≤2s 的定义量测点是 workflow_schedule_ms；这条腿不跑，报表那一行就永远"未测得"。"""
    from scripts.metrics_report import _table

    block = await workflow_leg(container, rounds=3)
    assert block["实例"] == 3
    assert sum(block["终态"].values()) == 3, "每个实例都要落到一个可归因的终态"
    stats = container.tracer.ledger.stats("workflow_schedule_ms")
    assert stats.count > 0, "节点就绪→开始执行必须真落账"
    row = next(
        r
        for r in _table(container.latency_report(), container.latency_report()["collaboration"])
        if r["指标"] == "常规任务调度响应时延 ≤2s"
    )
    assert row["样本"] == stats.count and row["判定"] != "未测得（账本里没有这条指标的样本）"


async def test_脚本代签核签必须单独计数(container) -> None:
    """灾种剧本以指挥员核签收尾：脚本签的就写成脚本签的，别让"无人签字"读成"签过了"。"""
    from aegis.workflow.templates import REPORT_REVIEW_TEMPLATE

    block = await workflow_leg(container, rounds=2, names=[REPORT_REVIEW_TEMPLATE["name"]])
    assert block["实例"] == 2
    assert block["脚本代签核签"] == 2, "核签流程每个实例都会停一次，代签数要与实例数对齐"


async def test_关掉的腿与缺模板都要留话(container) -> None:
    off = await workflow_leg(container, rounds=0)
    assert off["实例"] == 0 and "未开" in off["说明"]
    empty = await workflow_leg(container, rounds=2, names=[])
    assert empty["实例"] == 0 and "没有可用模板名" in empty["说明"]
    broken = await workflow_leg(container, rounds=1, names=["这套剧本不存在"])
    assert broken["实例"] == 0 and "工作流腿中断" in broken["说明"], "模板缺位不能静默，也不许把整张报表带崩"


def test_总线里mock与real并存就是抢答风险() -> None:
    """实测踩过：后端容器（默认 mock）与主机 --runtime real 连着同一条 JetStream，
    assess/plan 被 mock 毫秒级应答，真内环同时也在跑并烧 token——两侧都不是"agent 模式"的口径。"""
    snapshot = [
        {"agent_id": "assess.mock01", "agent_type": "assess"},
        {"agent_id": "assess.real01", "agent_type": "assess"},
        {"agent_id": "plan.real01", "agent_type": "plan"},
    ]
    assert mixed_registrations(snapshot) == ["assess"]


def test_单一副身子不算风险() -> None:
    assert mixed_registrations([{"agent_id": "assess.real01", "agent_type": "assess"}]) == []
    assert mixed_registrations([{"agent_id": "assess.mock01", "agent_type": "assess"}]) == []
    assert mixed_registrations([{"agent_id": "odd", "agent_type": "assess"}]) == [], "认不出后缀就不下断言"


def test_抢答时真栈也不给亮绿灯() -> None:
    settings = resolve_settings(
        bus_backend="nats",
        store_backend="postgres",
        agent_runtime="real",
        seed=1,
        environ={"AEGIS_NATS_URL": "nats://x:4222", "AEGIS_PG_DSN": "postgresql://y"},
    )
    contested = [{"agent_id": "plan.mock01", "agent_type": "plan"}, {"agent_id": "plan.real01", "agent_type": "plan"}]
    block = measurement_environment(settings, llm_configured=True, real_agents=5, mock_agents=0, registry_snapshot=contested)
    assert block["抢答风险"] == ["plan"]
    assert block["可作出口数字"] is False, "环境齐了但有人在抢答，数仍不可引用"
    assert "抢答" in block["口径提醒"]
    clean = measurement_environment(
        settings,
        llm_configured=True,
        real_agents=5,
        mock_agents=0,
        registry_snapshot=[{"agent_id": f"{t}.real01", "agent_type": t} for t in ("perceive", "assess", "plan", "execute", "feedback")],
    )
    assert clean["可作出口数字"] is True
    assert clean["在册智能体"]["总线视角总数"] == 5
