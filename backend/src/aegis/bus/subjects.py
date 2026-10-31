"""Subject 命名规范与 NATS 语义的通配匹配（规范 §2）。"""

from __future__ import annotations

from aegis.domain.enums import AgentType

TOKEN_RE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-")


def agent_in(agent_type: AgentType | str, agent_id: str = "") -> str:
    """智能体的收件 subject。

    带 `agent_id` 时是**该本体专属收件箱**（`agent.<type>.in.<agent_id>`）：网关照选中的
    那一个派发，同类型的其他本体收不到，侧效应不会被执行两遍。不带 id 时是类型级的
    公共收件口（`agent.<type>.in`）——保留给"只知道类型"的外部接入方与旧版发送方，
    该口上的定向请求由本体各自按 `request.target` 过滤。
    """
    base = f"agent.{agent_type.value if isinstance(agent_type, AgentType) else agent_type}.in"
    if not agent_id:
        return base
    subject = f"{base}.{agent_id}"
    if not is_valid_subject(subject):  # 大写等非法字符会在 NATS 侧静默收不到；在这里就地拒绝
        raise ValueError(f"agent_id 无法拼成合法 subject: {agent_id!r} -> {subject!r}")
    return subject


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
