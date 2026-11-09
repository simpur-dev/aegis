"""外部实现的**契约接入**判据（重构后分三层，各测各的）。

- `levels.py`：灾种/等级/状态的口径对照（外部语义的知识）；
- `contract_face.py`：契约 payload ↔ 外部调用参数、她的结果 ↔ 契约响应的翻译；
- `adapter.py`：只测契约壳的样板行为（动作分派、失败走 error 通道）。

**重点钉住三件曾经出过事的事**：
① 灾种键两套名字（平台 avalanche ↔ 外部 ice_rock_snow_avalanche）必须双向映射，
   否则那两个灾种的外部结论 100% 被丢弃；
② 认不出的档位**不给定级**（"算不出来" ≠ "没有风险"）；
③ 失败必须走 error 通道（塞进 response 会被网关当成功记账）。

放在平台测试目录（`tests/unit/`）而不是 `tests/unit/agents_ext/`：后者会被搬入脚本整目录清空。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.agents_ext.contract_face import (
    ExternalAgentError,
    assess_face,
    events_for,
    hazard_candidates,
    pick_hazard,
    region_of,
    task_of,
)
from aegis.agents_ext.levels import (
    CODE_ACTION_UNSUPPORTED,
    PLATFORM_TO_EXT_HAZARD,
    SOURCE_MARK,
    degraded_code,
    hazard_from_platform,
    hazard_to_platform,
    level_from_ext,
    level_to_ext,
)


class Test灾种口径:
    def test_平台两个不同名的灾种能映射到外部(self) -> None:
        """平台 avalanche ↔ 外部 ice_rock_snow_avalanche；lake_outburst ↔ glacial_lake_outburst。

        没有这张表，这两个灾种走外部实现时结论 100% 被丢弃（HazardType 抛 ValueError）。
        """
        assert hazard_from_platform("avalanche") == "ice_rock_snow_avalanche"
        assert hazard_from_platform("lake_outburst") == "glacial_lake_outburst"
        assert hazard_to_platform("ice_rock_snow_avalanche") == "avalanche"
        assert hazard_to_platform("glacial_lake_outburst") == "lake_outburst"

    def test_同名灾种原样映射(self) -> None:
        for key in ("landslide", "rockfall", "debris_flow"):
            assert hazard_from_platform(key) == key
            assert hazard_to_platform(key) == key

    def test_平台独有的灾种外部不支持_返回None不硬塞(self) -> None:
        assert hazard_from_platform("quake_triggered") is None
        assert hazard_from_platform("unknown") is None

    def test_外部认不出的灾种返回None(self) -> None:
        assert hazard_to_platform("火山泥流") is None
        assert hazard_to_platform("") is None

    def test_双向表由同一份导出_不手写第二份(self) -> None:
        """`PLATFORM_TO_EXT_HAZARD` 是**平台 → 外部**方向（名字即方向）。"""
        for platform_key, ext_key in PLATFORM_TO_EXT_HAZARD.items():
            assert hazard_from_platform(platform_key) == ext_key
            assert hazard_to_platform(ext_key) == platform_key


class Test等级口径:
    def test_外部档位映射到平台等级(self) -> None:
        assert level_from_ext("high") == 1
        assert level_from_ext("medium") == 2
        assert level_from_ext("low") == 3

    def test_认不出的档位不给定级_不给就等于无风险(self) -> None:
        """平台的 5 是"无风险"，把"算不出来"写成 5 就是"拿不到就说没有"。"""
        assert level_from_ext("unknown") is None
        assert level_from_ext("极端") is None
        assert level_from_ext(None) is None
        assert level_from_ext("") is None

    def test_平台的蓝与无没有外部对应档(self) -> None:
        assert level_to_ext(4) is None
        assert level_to_ext(5) is None
        assert level_to_ext(1) == "high"

    def test_每种状态各有自己的错误码且不过长(self) -> None:
        """错误码要过 `agent_message.v1.schema.json` 的 code pattern（E 后 ≤20 字符）。"""
        import re

        for status in ("insufficient_data", "uncertain", "failed"):
            code, _reason = degraded_code(status, {})
            assert re.match(r"^E[A-Z0-9_]{3,20}$", code), f"{code} 过不了 schema 的 code pattern"


class Test契约翻译:
    def test_灾种候选按顺序取_能映射的第一个(self) -> None:
        payload = {"candidate_hazards": ["quake_triggered", "avalanche", "debris_flow"]}
        assert pick_hazard(payload) == "ice_rock_snow_avalanche"

    def test_全都不支持时返回空串_不硬塞近似灾种(self) -> None:
        assert pick_hazard({"candidate_hazards": ["quake_triggered"]}) == ""
        assert pick_hazard({}) == ""

    def test_候选列表与单字段都能取(self) -> None:
        assert hazard_candidates({"hazard_type": "landslide"}) == ["landslide"]
        assert hazard_candidates({"candidate_hazards": ["landslide", "rockfall"]}) == ["landslide", "rockfall"]

    def test_区域取自region_code(self) -> None:
        assert region_of({"region_code": "540121"}) == "540121"

    def test_任务串优先用显式给的_否则按区域现编(self) -> None:
        assert task_of({"task": "复核泥石流"}, "540121") == "复核泥石流"
        assert "540121" in task_of({}, "540121")
        assert task_of({}, "") == "研判全域风险"

    def test_hits被转成事件骨架_并说明它不是观测(self) -> None:
        """平台给的是**规则命中**，不是观测——转换要如实标出来。"""
        data = events_for(
            {
                "hits": [
                    {
                        "rule_id": "R1",
                        "hazard_type": "debris_flow",
                        "region_code": "540121",
                        "score": 0.8,
                        "observed_at": "2026-11-09T00:00:00Z",
                    }
                ]
            }
        )
        assert data["processed_data"]["events"][0]["event_id"] == "R1"
        assert data["status"] == "success"
        assert data["processed_data"]["data_source"] == "platform_hits"

    def test_没有数据时如实说缺_不假装有观测(self) -> None:
        data = events_for({})
        assert data["status"] == "insufficient_data"
        assert data["missing_data"], "要说清缺什么"

    def test_已给data_result时原样用(self) -> None:
        given = {"processed_data": {"events": [{"event_id": "E1"}]}}
        assert events_for({"data_result": given}) is given


def _runner(result: dict[str, Any]) -> Any:
    def _call(**kwargs: Any) -> dict[str, Any]:
        return result

    return _call


class Test研判翻译:
    def test_成功时给出平台要的全部字段(self) -> None:
        result = assess_face(
            {"region_code": "540121", "candidate_hazards": ["debris_flow"]},
            _runner({"status": "success", "risk_level": "high", "confidence": 0.8, "reasoning": "强降雨"}),
        )
        assert result["risk_level"] == 1
        assert result["hazard_type"] == "debris_flow"
        assert result["region_code"] == "540121"
        assert result["confidence"] == 0.8
        assert result["rationale"] == "强降雨"
        assert result["assessed_by"] == "assess.agents_ext", "台账要能分清是谁答的"
        assert result["source"] == SOURCE_MARK
        assert "double_blind" in result, "双盲腿没做也要显式写，不留空被读成没评估"

    def test_数据不足抛错_不产定级(self) -> None:
        with pytest.raises(ExternalAgentError) as exc:
            assess_face(
                {"region_code": "540121", "candidate_hazards": ["debris_flow"]},
                _runner({"status": "insufficient_data", "missing_info": ["rainfall"]}),
            )
        assert exc.value.error_code == "E_EXT_INSUFFICIENT"
        assert "rainfall" in exc.value.message, "原因要带她的原话"

    def test_档位认不出抛错_不当成无风险(self) -> None:
        with pytest.raises(ExternalAgentError) as exc:
            assess_face(
                {"candidate_hazards": ["debris_flow"]},
                _runner({"status": "success", "risk_level": "unknown", "confidence": 0.5}),
            )
        assert exc.value.error_code == "E_EXT_LEVEL_UNKNOWN"

    def test_没有置信度就不产结论(self) -> None:
        """填 0.0 会被平台当成一条有效研判采纳——那是"编一个数冒充结论"。"""
        with pytest.raises(ExternalAgentError) as exc:
            assess_face(
                {"candidate_hazards": ["debris_flow"]},
                _runner({"status": "success", "risk_level": "high"}),
            )
        assert exc.value.error_code == "E_EXT_NO_CONFIDENCE"

    def test_灾种不支持时明确拒绝(self) -> None:
        with pytest.raises(ExternalAgentError) as exc:
            assess_face({"candidate_hazards": ["quake_triggered"]}, _runner({"status": "success"}))
        assert exc.value.error_code == "E_EXT_HAZARD_UNSUPPORTED"

    def test_内部异常收敛成错误并保留异常链(self) -> None:
        def _boom(**kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("内部炸了")

        with pytest.raises(ExternalAgentError) as exc:
            assess_face({"candidate_hazards": ["debris_flow"]}, _boom)
        assert exc.value.error_code == "E_EXT_FAILED"
        assert exc.value.retryable is True
        assert isinstance(exc.value.__cause__, RuntimeError)


class Test适配层只做壳:
    def test_依赖在位时可用(self) -> None:
        from aegis.agents_ext.adapter import available

        assert available() is True

    async def test_未接入的动作明确拒绝而不是猜着做(self) -> None:
        from aegis.agents_ext.adapter import ExternalAgentAdapter
        from aegis.domain.enums import AgentType
        from aegis.domain.messages import make_request

        class _FakeTransport:
            name = "memory"

            async def publish(self, subject: str, message: Any) -> None:
                return None

        adapter = ExternalAgentAdapter(
            _FakeTransport(),  # type: ignore[arg-type]
            agent_type=AgentType.PLAN,
            agent_id="plan.ext01",
            capabilities=["task_decompose"],
            action="plan.stu",
        )
        request = make_request(
            source="platform.gateway",
            target="plan.ext01",
            action="plan.stu",
            payload={},
            trace_id="trc_0123456789abcdef",
            reply_to="reply.gateway.inbox",
            deadline_ms=10_000,
        )
        with pytest.raises(ExternalAgentError) as exc:
            await adapter.handle(request)
        assert exc.value.error_code == CODE_ACTION_UNSUPPORTED

    def test_注册的动作对着平台真实下发的动作(self) -> None:
        """平台往 FEEDBACK 段发的是 feedback.attribute；feedback.status 是平台自己广播的事件。"""
        from aegis.agents_ext.adapter import build_ext_agents

        class _FakeTransport:
            name = "memory"

            async def publish(self, subject: str, message: Any) -> None:
                return None

        agents = build_ext_agents(_FakeTransport())  # type: ignore[arg-type]
        registered = {agent.agent_type.value: agent._action for agent in agents}
        assert registered["feedback"] == "feedback.attribute"
        assert registered["perceive"] == "assess.hazard"
        assert registered["assess"] == "assess.hazard"
