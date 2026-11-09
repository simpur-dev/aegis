"""适配层（`aegis.agents_ext.adapter`）的判据。

钉住"接口统一"这件事的四个要点：
1. **对外只认平台契约**：适配层实现 `AgentKit.handle(AgentMessage) -> Reply`，
   平台侧看不见她的 `run_xxx_agent` 签名；
2. **风险等级口径唯一**：她的 high/medium/low ↔ 平台的 1–5 双向映射，认不出的落到"无风险"，
   不臆造第三套口径；
3. **数据出处如实外标**：她的数据工具是内存 Mock 源，响应里必须带 `source=agents_ext_mock`，
   读的人一眼能看出这次结论的数据出处；
4. **"没法判断"不能变成"没有风险"**：她的 `insufficient_data` / `uncertain` / `failed`
   一律走错误/降级回执，**不产出 risk_level**——缺数据 ≠ 无风险。

放在平台测试目录（`tests/unit/`）而不是 `tests/unit/agents_ext/`：后者会被搬入脚本整目录清空。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.agents_ext.adapter import (
    SOURCE_MARK,
    ExternalAgentAdapter,
    ExternalAgentsUnavailableError,
    available,
    level_from_ext,
    level_to_ext,
)


class _FakeTransport:
    """适配层不碰总线（handle 是同步入口），这里只是满足 AgentKit 的构造签名。"""

    name = "memory"

    async def publish(self, subject: str, message: Any) -> None:
        return None


class Test风险等级口径:
    def test_平台1到5映射到她的档位(self) -> None:
        assert level_to_ext(1) == "high"
        assert level_to_ext(2) == "medium"
        assert level_to_ext(5) == "none"

    def test_她的档位映射回平台1到5(self) -> None:
        assert level_from_ext("high") == 1
        assert level_from_ext("medium") == 2
        assert level_from_ext("none") == 5

    def test_认不出的档位落到无风险而不是猜一个(self) -> None:
        assert level_from_ext("极端") == 5
        assert level_from_ext(None) == 5
        assert level_from_ext("") == 5

    def test_越界的平台等级落到无风险(self) -> None:
        assert level_to_ext(0) == "none"
        assert level_to_ext(99) == "none"


class Test可用性:
    def test_依赖在位时可用(self) -> None:
        assert available() is True

    def test_依赖缺席时如实说不可用而不是抛错(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builtins

        real_import = builtins.__import__

        def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("aegis.agents_ext"):
                raise ImportError("模拟：可选 extra 未安装")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        assert available() is False
        with pytest.raises(ExternalAgentsUnavailableError):
            _adapter()


def _adapter() -> ExternalAgentAdapter:
    from aegis.domain.enums import AgentType

    return ExternalAgentAdapter(
        _FakeTransport(),  # type: ignore[arg-type]
        agent_type=AgentType.ASSESS,
        agent_id="assess.ext01",
        capabilities=["risk_assess"],
        action="assess.hazard",
    )


def _request(payload: dict[str, Any], action: str = "assess.hazard") -> Any:
    from aegis.domain.messages import make_request

    return make_request(
        source="platform.gateway",
        target="assess.ext01",
        action=action,
        payload=payload,
        trace_id="trc_0123456789abcdef",
        reply_to="reply.gateway.inbox",
        deadline_ms=10_000,
    )


def _data_result(hazard: str = "debris_flow") -> dict[str, Any]:
    """上游 Data 输出的最小结构（与她的 mock 构造器同形：`processed_data.events`）。

    观测字段取灾种注册表的 sample_values——**这是她的 Mock 数据源**，
    所以适配层出来的结论带 `source=agents_ext_mock`。
    """
    from aegis.agents_ext.risk_agent.tools.hazard_risk_registry import HAZARD_RISK_PROFILES

    profile = HAZARD_RISK_PROFILES[hazard]
    events = []
    for i in range(1, 3):
        event: dict[str, Any] = {
            "event_id": f"MOCK-{i:03d}",
            "time": "2024-07-08T03:00:00Z",
            "latitude": 29.6 + i * 0.01,
            "longitude": 95.3 + i * 0.01,
            "region": "西藏",
        }
        for field, value in profile.get("sample_values", {}).items():
            event[field] = value
        events.append(event)
    return {
        "processed_data": {"events": events, "data_source": "mock", "hazard_type": hazard, "is_mock": True},
        "data_quality": {"is_valid": True},
        "missing_data": [],
        "status": "success",
        "hazard_type": hazard,
    }


class Test适配:
    async def test_平台请求被翻译成她的调用并回契约响应(self) -> None:
        adapter = _adapter()
        reply = await adapter.handle(
            _request(
                {
                    "region_code": "540121",
                    "candidate_hazards": ["debris_flow"],
                    "data_result": _data_result("debris_flow"),
                }
            )
        )
        payload = reply.payload
        assert "error" not in payload, f"有完整观测数据时应给出定级：{payload.get('error')}"
        assert payload["hazard_type"] == "debris_flow"
        assert payload["region_code"] == "540121"
        assert 1 <= payload["risk_level"] <= 5
        assert payload["source"] == SOURCE_MARK, "Mock 数据源的结论必须标出处，不能冒充平台实测"

    async def test_数据不足不产出定级_缺数据不是无风险(self) -> None:
        """没有观测数据时她的 status 是 insufficient_data —— 那是"没法判断"。

        关键：**不能**映射成 risk_level=5（无风险）。那是"拿不到就说没有"，
        与本仓反复禁止的"读视图空 ≠ 没有数据"同一类错。
        """
        adapter = _adapter()
        reply = await adapter.handle(_request({"region_code": "540121", "candidate_hazards": ["debris_flow"]}))
        payload = reply.payload
        assert "error" in payload, "数据不足必须走错误/降级回执，不能当成有结论"
        assert payload["error"]["code"] == "E_EXTERNAL_AGENT_INSUFFICIENT_DATA"
        assert "risk_level" not in payload, "没有定级就不能给出 risk_level（缺数据 ≠ 无风险）"
        assert payload["degraded"] is True
        assert payload["region_code"] == "540121", "要带出是哪个范围没结论"

    async def test_灾种取不到时交给她自己fail_fast(self) -> None:
        """适配层不瞎猜灾种：灾种缺失时她的入口显式拒绝（insufficient_data）。"""
        adapter = _adapter()
        reply = await adapter.handle(_request({"region_code": "540121"}))
        assert "error" in reply.payload
        assert "risk_level" not in reply.payload

    async def test_动作不匹配时明确拒绝(self) -> None:
        adapter = _adapter()
        with pytest.raises(RuntimeError, match="不处理动作"):
            await adapter.handle(_request({}, action="execute.warn"))

    async def test_内部异常收敛成错误回执而不是穿出总线(self) -> None:
        adapter = _adapter()

        def _boom(**kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("内部炸了")

        adapter._runners["risk"] = _boom  # type: ignore[assignment]
        reply = await adapter.handle(
            _request(
                {
                    "region_code": "540121",
                    "candidate_hazards": ["debris_flow"],
                    "data_result": _data_result("debris_flow"),
                }
            )
        )
        assert reply.payload["error"]["code"] == "E_EXTERNAL_AGENT_FAILED"
        assert "内部炸了" in reply.payload["error"]["message"]
