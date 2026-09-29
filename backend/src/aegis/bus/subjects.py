"""Subject 命名规范与 NATS 语义的通配匹配（规范 §2）。"""

from __future__ import annotations

from aegis.domain.enums import AgentType

TOKEN_RE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-")


def agent_in(agent_type: AgentType | str) -> str:
    return f"agent.{agent_type.value if isinstance(agent_type, AgentType) else agent_type}.in"


def agent_out(agent_type: AgentType | str) -> str:
    return f"agent.{agent_type.value if isinstance(agent_type, AgentType) else agent_type}.out"


def agent_hb(agent_type: AgentType | str) -> str:
    return f"agent.{agent_type.value if isinstance(agent_type, AgentType) else agent_type}.hb"


def data(source: str, metric: str) -> str:
    return f"data.{source}.{metric}"


def workflow(instance_id: str, event: str) -> str:
    return f"workflow.{instance_id}.{event}"


def alert(level: int, region_code: str) -> str:
    return f"platform.alert.{level}.{region_code}"


def ops(component: str, signal: str) -> str:
    return f"ops.{component}.{signal}"


def reply(component: str = "gateway") -> str:
    """请求-响应的回执 subject；request.reply_to 取此值。"""
    if not all(c in TOKEN_RE_CHARS for c in component) or not component:
        raise ValueError(f"非法 reply 组件名: {component!r}")
    return f"reply.{component}.inbox"


def subject_matches(pattern: str, subject: str) -> bool:
    """NATS 语义：token '*' 匹配单层，token '>' 匹配其后全部剩余层。空 token 视为非法，不匹配。"""
    p_tokens = pattern.split(".")
    s_tokens = subject.split(".")
    if not subject or any(not token for token in s_tokens):
        return False
    for i, token in enumerate(p_tokens):
        if token == ">":
            return len(s_tokens) > i
        if i >= len(s_tokens):
            return False
        if token != "*" and token != s_tokens[i]:
            return False
    return len(p_tokens) == len(s_tokens)


def is_valid_subject(subject: str) -> bool:
    if not subject or subject.startswith(".") or subject.endswith("."):
        return False
    tokens = subject.split(".")
    return all(token and all(c in TOKEN_RE_CHARS for c in token) for token in tokens)
