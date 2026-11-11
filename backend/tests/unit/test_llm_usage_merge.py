"""用量出口必须把两本台账合起来算：网关随实例那本 + 灾种模块级那本。

【为什么要有这一组】
台账实现共享 ≠ 统计共享：`shared_ledger()` 此前零读者，灾种调用的用量/成本/首过率
在治理出口里整体缺席。这一组钉住合并后的口径：
  ① 聚合（calls/token/时延）含灾种行；② 首过率 by_task 含灾种 task；
  ③ 预算告警把灾种 agent 的 token 也算进去；④ 不传 extra 时行为与升级前逐字一致。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.agents.core.ledger import LlmCallRecord, record_call, reset_shared_ledger
from aegis.config import Settings
from aegis.container import PlatformContainer
from aegis.domain.messages import now_iso
from aegis.services.llm.llm_gateway import LlmGateway


def _row(**overrides: Any) -> LlmCallRecord:
    base: dict[str, Any] = {
        "task": "hazard.risk_explain",
        "model": "glm-4-flash",
        "prompt_version": "1.0.0",
        "temperature": 0.0,
        "max_tokens": 1024,
        "latency_ms": 120.0,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "validation": "ok",
        "trace_id": "trc_" + "a" * 16,
        "agent_id": "assess.factor01",
        "at": now_iso(),
        "structured": True,
        "attempt": 0,
    }
    base.update(overrides)
    return LlmCallRecord(**base)


@pytest.fixture(autouse=True)
def _clean_ledger():
    reset_shared_ledger()
    yield
    reset_shared_ledger()


def _gateway(**settings_overrides: Any) -> LlmGateway:
    return LlmGateway(settings=Settings(env="test", **settings_overrides), client=object())


def test_不传extra时与升级前一致() -> None:
    g = _gateway()
    snap = g.usage_snapshot()
    assert snap["total_calls"] == 0
    assert snap["by_agent_model"] == []
    assert snap["structured_first_pass"]["rate"] is None, "没跑过就是未评测，不冒满分"


def test_聚合含灾种行且形状不变() -> None:
    g = _gateway()
    extra = [_row(), _row(attempt=1, latency_ms=200.0), _row(validation="malformed", total_tokens=30)]
    snap = g.usage_snapshot(extra_records=extra)
    assert set(snap) == {"by_agent_model", "total_calls", "month", "budget_alerts", "structured_first_pass"}
    assert snap["total_calls"] == 3
    (bucket,) = snap["by_agent_model"]
    assert bucket["agent_id"] == "assess.factor01"
    assert bucket["model"] == "glm-4-flash"
    assert bucket["calls"] == 3
    assert bucket["total_tokens"] == 60, "修复轮（attempt=1）的 token 也计入成本——token 是真花了的；attempt 只改首过率分母，不改成本口径"
    assert bucket["validation"] == {"ok": 2, "malformed": 1}


def test_首过率by_task含灾种task() -> None:
    g = _gateway()
    extra = [_row(), _row(validation="malformed"), _row(attempt=1)]
    stats = g.usage_snapshot(extra_records=extra)["structured_first_pass"]
    assert stats["by_task"]["hazard.risk_explain"] == 0.5, "attempt=0 两行一过一修复；attempt=1 不进分母"
    assert stats["rate"] == 0.5


def test_预算告警算上灾种token() -> None:
    g = _gateway(llm_monthly_token_budget_default=20)
    alerts = g.usage_snapshot(extra_records=[_row(total_tokens=50)])["budget_alerts"]
    assert len(alerts) == 1
    assert alerts[0]["agent_id"] == "assess.factor01"
    assert alerts[0]["tokens"] == 50
    assert alerts[0]["action"] == "alert_only", "超限只告警不熔断（口径不变）"


async def test_平台用量出口并入灾种台账(container: PlatformContainer) -> None:
    """container 接线验证：latency_report 的 llm 块真的读了 shared_ledger。"""
    container.llm_gateway = _gateway()
    record_call(_row(agent_id="assess.data01", task="hazard.data_plan"))
    block = container.latency_report()["llm"]
    assert block["configured"] is True
    assert block["total_calls"] == 1
    assert block["by_agent_model"][0]["agent_id"] == "assess.data01"
    assert block["structured_first_pass"]["by_task"]["hazard.data_plan"] == 1.0


async def test_无灾种调用时出口不臆造(container: PlatformContainer) -> None:
    container.llm_gateway = _gateway()
    block = container.latency_report()["llm"]
    assert block["total_calls"] == 0
    assert block["by_agent_model"] == []
