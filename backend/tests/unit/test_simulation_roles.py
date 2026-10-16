"""§4.4 角色侧（`aegis.simulation.roles`）：白名单闸、LLM 档的降级口径、装配选档。

这一层的验收不是"推演准不准"（那是数值模型的单调性与抽查），而是三件事：
1. 角色结构上没法发明数字（schema 里没有数值字段）；
2. 模型腿抖了只塌那一轮，不把整份报告拖成 503，且回落要计数、可外显；
3. 没配 LLM 时走的确实是确定性档，报告里的 `roles.policy` 与实际用的那一档同源。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from aegis.config import Settings
from aegis.services.simulation import RulePolicy, Sandbox, SimConfig, make_sandbox
from aegis.simulation.roles import ACTIONS, ROLE_PROFILES, ROLES_BY_KEY, LlmRolePolicy, RoleAction, RoleProfile


class TestRoleProfiles:
    def test_four_paper_roles_are_registered(self) -> None:
        assert [p.key for p in ROLE_PROFILES] == ["commander", "rescue", "residents", "monitor"]
        assert set(ROLES_BY_KEY) == {p.key for p in ROLE_PROFILES}

    def test_every_profile_can_be_rendered(self) -> None:
        """报告页要显示角色名与立场；空 title 会让页面上出现只有 key 的哑行。"""
        for profile in ROLE_PROFILES:
            assert isinstance(profile, RoleProfile)
            assert profile.title and profile.concern
            assert set(profile.as_row()) == {"key", "title", "concern"}


class TestRoleActionSchema:
    @pytest.mark.parametrize("action", ACTIONS)
    def test_whitelist_actions_pass(self, action: str) -> None:
        assert RoleAction(action=action).action == action

    @pytest.mark.parametrize("action", ["组织群众转移", "evacuate", "躺平", "转移,封路", "转移/封路", ""])
    def test_synonyms_and_blanks_are_rejected(self, action: str) -> None:
        """同义改写正是模型最常回的写法：不逐字校验，白名单就只是提示词里的注释。"""
        with pytest.raises(ValidationError):
            RoleAction(action=action)

    def test_padding_around_a_whitelist_action_is_normalized(self) -> None:
        """逐字校验针对的是"档位集合"，不是"字节"：把 " 转移 " 打死只会白白多烧一次修复轮。"""
        assert RoleAction(action=" 转移 ").action == "转移"

    def test_extra_keys_are_forbidden(self) -> None:
        """没有数值字段可填 = 结构上不能发明数字；放行 extra 就等于把这道闸拆了。"""
        with pytest.raises(ValidationError):
            RoleAction.model_validate({"action": "转移", "rationale": "先转移", "probability": 0.42})

    def test_action_is_required(self) -> None:
        with pytest.raises(ValidationError):
            RoleAction.model_validate({"rationale": "只给了理由"})

    def test_rationale_length_capped(self) -> None:
        with pytest.raises(ValidationError):
            RoleAction(action="转移", rationale="字" * 201)


class _RecordingGateway:
    """够用的网关替身：`available` + 结构化调用，并按脚本返回或抛错。"""

    def __init__(self, *, replies: list[Any] | None = None, raises: bool = False) -> None:
        self.available = True
        self.raises = raises
        self._replies = list(replies or [])
        self.calls: list[dict[str, Any]] = []

    async def chat_structured(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        self.calls.append({"messages": messages, **kwargs})
        if self.raises:
            raise TimeoutError("模型腿抖")
        reply = self._replies.pop(0) if self._replies else RoleAction(action="封路")
        if isinstance(reply, BaseException):
            raise reply
        return reply


class TestLlmRolePolicy:
    async def test_uses_prompt_registry_and_task_simulation(self) -> None:
        gw = _RecordingGateway()
        policy = LlmRolePolicy(gw, trace_id="t-1")
        action = await policy.choose(role="rescue", round_index=2, intensity=4.5, preferred="转移")
        assert action == "封路"
        assert policy.calls == 1 and policy.fallbacks == 0
        sent = gw.calls[0]
        assert sent["task"] == "simulation" and sent["prompt_key"] == "simulation"
        assert sent["agent_id"] == "sim.rescue" and sent["trace_id"] == "t-1"
        assert sent["schema"] is RoleAction
        # 白名单必须逐字进提示词（assess/plan 都栽过"只说取其一"的坑）。
        assert sent["prompt_vars"]["actions"] == "、".join(ACTIONS)
        assert sent["prompt_vars"]["preferred"] == "转移"
        assert sent["prompt_vars"]["role"] == "救援队"
        assert sent["prompt_vars"]["intensity"] == "4.5cm"

    async def test_timeout_falls_back_to_the_preferred_action_and_counts(self) -> None:
        gw = _RecordingGateway(raises=True)
        policy = LlmRolePolicy(gw)
        assert await policy.choose(role="commander", round_index=0, intensity=1.0, preferred="转移") == "转移"
        assert policy.calls == 1 and policy.fallbacks == 1

    @pytest.mark.parametrize(
        "payload",
        [
            {"action": "组织群众转移"},
            {"action": "转移", "probability": 0.42},
            {"rationale": "只给了理由"},
            "转移",
            None,
        ],
    )
    async def test_a_raw_payload_that_is_not_a_valid_action_collapses_this_round(self, payload: Any) -> None:
        """schema 闸装在网关实现里；这一档自己再过一遍，才防得住替身/降级路径递来的野票。"""
        gw = _RecordingGateway(replies=[payload])
        policy = LlmRolePolicy(gw)
        assert await policy.choose(role="monitor", round_index=0, intensity=1.0, preferred="转移") == "转移"
        assert policy.calls == 1 and policy.fallbacks == 1

    async def test_valid_dict_payload_is_accepted(self) -> None:
        gw = _RecordingGateway(replies=[{"action": "封路", "rationale": "通路先封"}])
        policy = LlmRolePolicy(gw)
        assert await policy.choose(role="monitor", round_index=0, intensity=1.0, preferred="转移") == "封路"
        assert policy.fallbacks == 0

    async def test_unknown_role_never_burns_a_model_call(self) -> None:
        gw = _RecordingGateway()
        policy = LlmRolePolicy(gw)
        assert await policy.choose(role="ghost", round_index=0, intensity=1.0, preferred="转移") == "转移"
        assert gw.calls == [] and policy.calls == 0 and policy.fallbacks == 1

    async def test_no_gateway_is_honest_fallback_not_a_silent_zero(self) -> None:
        policy = LlmRolePolicy(None)  # type: ignore[arg-type]
        assert await policy.choose(role="rescue", round_index=0, intensity=1.0, preferred="封路") == "封路"
        assert policy.fallbacks == 1

    async def test_sandbox_reports_how_many_rounds_were_actually_asked(self) -> None:
        gw = _RecordingGateway()
        cfg = SimConfig(rounds=2)
        report = await Sandbox(policy=LlmRolePolicy(gw)).run(cfg)
        asked = 2 * len(cfg.roles)
        assert report.roles["policy"] == "llm"
        assert report.roles["calls"] == asked == len(gw.calls)
        assert report.roles["fallbacks"] == 0
        assert "note" not in report.roles


class TestMakeSandbox:
    def test_default_is_the_deterministic_policy(self) -> None:
        sandbox = make_sandbox(Settings(env="test"))
        assert isinstance(sandbox.policy, RulePolicy)

    def test_llm_switch_needs_both_flag_and_available_gateway(self) -> None:
        cfg = Settings(env="test", simulation_roles_llm=True)
        assert isinstance(make_sandbox(cfg).policy, RulePolicy), "开关开了但网关不在位 → 仍走确定性档"
        assert isinstance(make_sandbox(cfg, llm_gateway=_RecordingGateway()).policy, LlmRolePolicy)

    def test_unavailable_gateway_does_not_become_an_llm_run(self) -> None:
        class _Off:
            available = False

        sandbox = make_sandbox(Settings(env="test", simulation_roles_llm=True), llm_gateway=_Off())
        assert isinstance(sandbox.policy, RulePolicy)

    def test_each_call_gets_a_fresh_policy_so_counters_are_not_shared(self) -> None:
        """两个推演请求复用同一个 policy 时，第二份报告的 fallbacks 会带上第一次的回落数。"""
        cfg = Settings(env="test", simulation_roles_llm=True)
        first, second = make_sandbox(cfg), make_sandbox(cfg)
        assert first.policy is not second.policy
        gw = _RecordingGateway()
        llm_first = make_sandbox(cfg, llm_gateway=gw)
        assert isinstance(llm_first.policy, LlmRolePolicy)
        assert llm_first.policy.calls == 0
