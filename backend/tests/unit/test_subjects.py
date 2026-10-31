"""Subject 规范与 NATS 通配匹配语义测试（含通配边界）。"""

from __future__ import annotations

import pytest

from aegis.bus import subjects
from aegis.domain.enums import AgentType


class TestBuilders:
    def test_agent_subjects(self) -> None:
        assert subjects.agent_in(AgentType.ASSESS) == "agent.assess.in"
        assert subjects.agent_out("perceive") == "agent.perceive.out"
        assert subjects.agent_hb(AgentType.PLAN) == "agent.plan.hb"

    def test_agent_inbox_is_per_agent_and_extends_the_family_subject(self) -> None:
        """本体收件箱是类型级公开口的下级层：两部订阅互不串味，流通配 `agent.*.in.>` 能盖住它。"""
        inbox = subjects.agent_in(AgentType.ASSESS, "assess.real01")
        assert inbox == "agent.assess.in.assess.real01"
        assert subjects.subject_matches("agent.*.in.>", inbox)
        assert not subjects.subject_matches("agent.*.in", inbox), "公共口不该收到专属收件箱的消息"

    def test_agent_in_rejects_unaddressable_id(self) -> None:
        """拼不出合法 token 的 id 会在 NATS 上静默收不到：构造期就拒绝，而不是上线后靠现象猜。"""
        with pytest.raises(ValueError, match="subject"):
            subjects.agent_in(AgentType.ASSESS, "Assess.Real01")

    def test_data_and_workflow(self) -> None:
        assert subjects.data("RG-01", "rain_10min") == "data.RG-01.rain_10min"
        assert subjects.workflow("wfi_abc", "started") == "workflow.wfi_abc.started"

    def test_alert_and_ops(self) -> None:
        assert subjects.alert(1, "540121") == "platform.alert.1.540121"
        assert subjects.ops("feedback", "status") == "ops.feedback.status"

    def test_reply_inbox_shape(self) -> None:
        assert subjects.reply("gw0001") == "reply.gw0001.inbox"

    def test_reply_rejects_invalid_component(self) -> None:
        with pytest.raises(ValueError):
            subjects.reply("Bad Name")


class TestMatching:
    @pytest.mark.parametrize(
        ("pattern", "subject", "expected"),
        [
            ("agent.assess.in", "agent.assess.in", True),
            ("agent.assess.in", "agent.assess.out", False),
            ("agent.*.in", "agent.plan.in", True),
            ("agent.*.in", "agent.plan.out", False),
            ("agent.*.in", "agent.plan.deep.in", False),
            ("data.>", "data.rg.rain", True),
            ("data.>", "data.a.b.c", True),
            ("data.>", "platform.alert.1", False),
            ("data.>", "data.", False),
            ("data.x.*", "data.x.y", True),
            ("data.x.*", "data.x", False),
            ("workflow.wfi_1.>", "workflow.wfi_1.started", True),
            ("workflow.wfi_1.>", "workflow.wfi_2.started", False),
        ],
    )
    def test_wildcards(self, pattern: str, subject: str, expected: bool) -> None:
        assert subjects.subject_matches(pattern, subject) is expected

    def test_greater_at_root_matches_any_depth(self) -> None:
        assert subjects.subject_matches(">", "a")
        assert subjects.subject_matches(">", "a.b.c")

    def test_exact_pattern_does_not_match_prefix(self) -> None:
        assert not subjects.subject_matches("agent", "agent.assess.in")


class TestSubjectValidity:
    @pytest.mark.parametrize(
        ("subject", "expected"),
        [
            ("agent.assess.in", True),
            ("data.rg_01.rain_10min", True),
            ("platform.alert.1.540121", True),
            ("", False),
            (".leading", False),
            ("trailing.", False),
            ("UPPER.case", False),
            ("has space.token", False),
        ],
    )
    def test_is_valid_subject(self, subject: str, expected: bool) -> None:
        assert subjects.is_valid_subject(subject) is expected
