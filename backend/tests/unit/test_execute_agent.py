"""execute 内环（§3.4）的口径：三分支文案 + 数字必须有出处，而且绝不自称送达。

这里没有真 LLM：网关用替身顶掉，断言的是内环自己的两条纪律——
① 每条受众分支的数字化逐字比对事实池（`content_refs` + 平台既有文案），冒出无出处数字的分支
   整条弃用并记进 `rejected_roles`，不"润色"成看起来安全的版本；
② 响应面里没有 `channel_results`、没有 `released_at`：发送与回执属于平台交付腿
   （`pipeline.chain._execute` 那侧的用例另证）。
"""

from __future__ import annotations

from typing import Any

import pytest

from aegis.agents.agentkit import Reply
from aegis.agents.execute import ExecuteAgent, ExecuteBranches
from aegis.domain.enums import AgentType
from aegis.domain.messages import make_request

TRACE = "trc_" + "0" * 16
FACTS = "R-DEBRIS-RAIN-1=0.9 10 分钟降雨 32 毫米，泥位 1.4 米"
PLATFORM_BODY = "10 分钟降雨 32 毫米，请立即避险"


class FakeGateway:
    def __init__(
        self,
        result: ExecuteBranches | None = None,
        *,
        translated: str = "བོད་ཡིག་ཁྱབ་བསྒྲགས།",
        translate_error: Exception | None = None,
    ) -> None:
        self.result = result or ExecuteBranches()
        self.translated = translated
        self.translate_error = translate_error
        self.calls: list[dict[str, Any]] = []
        self.translate_calls: list[str] = []
        self.translate_agent_id: Any = None

    async def chat_structured(self, messages: list[dict[str, str]], **kwargs: Any) -> ExecuteBranches:
        self.calls.append(kwargs)
        return self.result

    async def translate(
        self,
        text: str,
        *,
        target_language: str = "藏语（卫藏方言，书面语）",
        agent_id: str | None = None,
        trace_id: str | None = None,
    ) -> str:
        self.translate_calls.append(text)
        self.translate_agent_id = agent_id
        if self.translate_error is not None:
            raise self.translate_error
        return self.translated


def agent(gateway: Any | None = None) -> ExecuteAgent:
    return ExecuteAgent(
        transport=None,
        agent_type=AgentType.EXECUTE,
        agent_id="execute.t01",
        capabilities=["warn_publish"],
        llm=gateway,
    )  # type: ignore[arg-type]


def request(payload: dict[str, Any]) -> Any:
    return make_request(
        source="platform.t01",
        target="execute.t01",
        action="execute.warn",
        payload=payload,
        trace_id=TRACE,
        reply_to="reply.t01.inbox",
        deadline_ms=5000,
    )


def warn_payload(**over: Any) -> dict[str, Any]:
    base = {
        "warning_id": "wrn_t01",
        "level": 1,
        "regions": ["540121"],
        "audiences": ["residents", "decision_lead"],
        "content": {"zh": PLATFORM_BODY, "bo": None},
        "channels": ["sms", "broadcast"],
        "content_refs": FACTS,
    }
    return {**base, **over}


async def test_没有网关时回落平台文案并如实说明降级() -> None:
    reply: Reply = await agent(None).handle(request(warn_payload()))
    body = reply.payload
    assert body["body_zh"] == PLATFORM_BODY
    assert body["audience_copies"] == {} and body["rejected_roles"] == []
    assert "未配置 LLM 网关" in body["degraded"]
    assert body["translation_pending"] is True, "没有藏文又没人能翻，就得标 pending"


async def test_三分支数字都有出处就全采纳() -> None:
    gateway = FakeGateway(
        ExecuteBranches(
            title_zh="泥石流红色预警",
            branches={
                "decision_lead": "请核查 540121 责任面：10 分钟降雨 32 毫米，泥位 1.4 米",
                "responder": "按处置预案时限到位，装备依短雨 32 毫米等级配载",
                "residents": "立即沿预定路线避险（短雨 32 毫米）",
            },
        )
    )
    reply = await agent(gateway).handle(request(warn_payload()))
    body = reply.payload
    assert body["title_zh"] == "泥石流红色预警"
    assert set(body["audience_copies"]) == {"decision_lead", "responder", "residents"}
    assert body["body_zh"] == "立即沿预定路线避险（短雨 32 毫米）", "公共触达面取民众分支"
    assert body["rejected_roles"] == []
    assert gateway.calls[0]["prompt_key"] == "execute" and gateway.calls[0]["task"] == "execute"
    assert FACTS in gateway.calls[0]["prompt_vars"]["facts"], "事实池必须把 content_refs 交下去"


async def test_无出处数字的分支整条弃用并记下是谁() -> None:
    """变异点：若闸只改写不丢弃，这里会拿到 88.8 那条；若只丢不记，rejected_roles 会空。"""
    gateway = FakeGateway(
        ExecuteBranches(
            title_zh="泥石流红色预警",
            branches={
                "decision_lead": "10 分钟降雨 32 毫米，另有 88.8 毫米预计雨量",
                "residents": "立即避险（短雨 32 毫米）",
            },
        )
    )
    reply = await agent(gateway).handle(request(warn_payload()))
    body = reply.payload
    assert "decision_lead" not in body["audience_copies"]
    assert body["rejected_roles"] == ["decision_lead"]
    assert body["body_zh"] == "立即避险（短雨 32 毫米）"


async def test_民众分支被拒时正文回落平台模板而不是别的受众分支() -> None:
    gateway = FakeGateway(
        ExecuteBranches(title_zh="标题", branches={"residents": "预计 88.8 毫米，快撤离", "decision_lead": "依据 32 毫米"})
    )
    reply = await agent(gateway).handle(request(warn_payload()))
    assert reply.payload["body_zh"] == PLATFORM_BODY
    assert reply.payload["rejected_roles"] == ["residents"]


async def test_响应面不含任何送达声明() -> None:
    """这条是整个改动的正面陈述：execute 的 ack 里没有 channel_results，也没有 released_at。"""
    gateway = FakeGateway(ExecuteBranches(title_zh="t", branches={"residents": "短雨 32 毫米，立即避险"}))
    reply = await agent(gateway).handle(request(warn_payload()))
    assert "channel_results" not in reply.payload
    assert "released_at" not in reply.payload
    assert reply.payload["warning_id"] == "wrn_t01"


async def test_已有藏文时沿用且不再翻() -> None:
    gateway = FakeGateway(ExecuteBranches(title_zh="t", branches={"residents": "短雨 32 毫米，立即避险"}))
    reply = await agent(gateway).handle(request(warn_payload(content={"zh": PLATFORM_BODY, "bo": "བོད་ཡིག"})))
    assert reply.payload["body_bo"] == "བོད་ཡིག"
    assert reply.payload["translation_pending"] is False
    assert gateway.translate_calls == []


async def test_翻译腿成功则补上藏文() -> None:
    gateway = FakeGateway(ExecuteBranches(title_zh="t", branches={"residents": "短雨 32 毫米，立即避险"}))
    reply = await agent(gateway).handle(request(warn_payload()))
    assert reply.payload["body_bo"] == "བོད་ཡིག་ཁྱབ་བསྒྲགས།"
    assert reply.payload["translation_pending"] is False
    assert gateway.translate_calls == ["短雨 32 毫米，立即避险"], "翻的是采纳后的民众分支，不是被拒的草稿"
    assert gateway.translate_agent_id == "execute.t01", "藏文腿的 token 必须记在 execute 名下（§4.8.3 按智能体预算）"


@pytest.mark.parametrize("boom", [RuntimeError("网关超时"), ValueError("bad")])
async def test_翻译腿失败时标pending而不是空串冒充(boom: Exception) -> None:
    """变异点：把异常吞成空字符串会被下游读成"已经翻译但内容为空"；pending 才是事实。"""
    gateway = FakeGateway(ExecuteBranches(title_zh="t", branches={"residents": "短雨 32 毫米"}), translate_error=boom)
    reply = await agent(gateway).handle(request(warn_payload()))
    assert reply.payload["body_bo"] is None
    assert reply.payload["translation_pending"] is True


async def test_空分支与空标题都不算生成过() -> None:
    gateway = FakeGateway(ExecuteBranches(title_zh="   ", branches={"residents": "  ", "decision_lead": "依据 32 毫米"}))
    reply = await agent(gateway).handle(request(warn_payload()))
    assert reply.payload["title_zh"] == ""
    assert set(reply.payload["audience_copies"]) == {"decision_lead"}
    assert reply.payload["rejected_roles"] == [], "空文本是没写，不是写了违规数字"


class TestProvenance:
    """D10 出处凭证：execute 也要说清"这段文案是谁写的"——它是民众读到的那一段。

    这一组是被实时读数逼出来的：2026-10-12 重启真栈跑一条上报，链台账里
    assess/plan 两段都带 `{model, prompt_version}`，唯独 `mode=agent` 的 execute 段是空的——
    不是链路没转记，是 execute 内环压根没把凭证放进信封。
    """

    async def test_走模型时带出模型与prompt版本(self) -> None:
        class Provenanced(FakeGateway):
            def model_info_for_task(self, task: str) -> dict[str, Any]:
                return {"model": "deepseek-chat", "prompt_version": "1.0.0"}

        gateway = Provenanced(
            ExecuteBranches(
                title_zh="泥石流橙色预警",
                branches={"residents": "立即沿预定路线避险（短雨 32 毫米）", "decision_lead": "请核查 540121 责任面"},
            )
        )
        reply: Reply = await agent(gateway).handle(request(warn_payload()))
        assert reply.model_info is not None, "采纳智能体文案却不记出处，README 那句『可审计』就还是半真"
        assert reply.model_info.model == "deepseek-chat"
        assert reply.model_info.prompt_version == "1.0.0"

    async def test_回落轨不编凭证(self) -> None:
        """没网关时 execute 原样回吐平台文案：这一段没有模型参与，凭证必须缺席而不是填个『unknown』。"""
        reply: Reply = await agent(None).handle(request(warn_payload()))
        assert reply.model_info is None
