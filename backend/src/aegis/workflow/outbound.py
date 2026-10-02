"""工作流外呼的唯一出口（`api_call` / `device_control` 两类节点用它）。

为什么要单独一层、而且默认关闭：这两个节点的目标 URL 是**在编排画布上填的**，
等于让流程定义者决定平台往哪儿发请求。不设闸就是 SSRF 面——内网元数据端点、
旁路服务都只差一次点击。另一头同样是真的问题：装配桥此前从未注入 `http_call`，
于是 16 类节点里这两类在产品形态下必然 `NodeError`（2026-10-02 独立审计实测），
"支持 ≥10 类节点"那句话在这两点上是虚的。口径因此定为**配了主机白名单才开，
没配就响亮地不可用**，而不是"默认能发"。

两条边界照实写在这里，别以为白名单盖住了所有情况：
① 只按主机名精确匹配（不比端口、不比路径），DNS 重绑定不在这一层防护范围内；
② 一律不跟随重定向，否则一个白名单主机就能把请求转到任意内网地址。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from aegis.errors import AegisError, ErrorCode

if TYPE_CHECKING:
    from aegis.config import Settings

# 允许的方法与协议：写死在这里，而不是让节点配置去决定"能不能发"。
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
ALLOWED_SCHEMES = frozenset({"http", "https"})

REASON_DISABLED = "外呼未启用：没有配置任何允许的主机白名单"


class OutboundTargetError(AegisError):
    """外呼目标不合法或被策略拒绝：属于定义期错误，不重试（重试也还是同一个非法目标）。"""

    code = ErrorCode.DATA_FORBIDDEN


def normalize_host(entry: str) -> str | None:
    """把一条白名单条目收敛成主机名——**与匹配侧用同一条规则**。

    运维写 `https://api.example.com/v1/`、`user:pw@host`、`host:8443` 都是常见笔误。这里直接借
    `urlsplit().hostname` 取值，好处是白名单的规范化口径与 `refusal_reason` 里比对目标 URL 的
    口径是同一份代码：两份各写一半时，"配了白名单却永远匹配不上"这种故障几乎查不出来。
    凭据与端口一律不进结果：白名单是主机粒度的，而这条列表会原样进
    `GET /api/v1/integrations`（匿名可读），留 userinfo 等于把口令贴出去。
    """
    text = str(entry or "").strip()
    if not text:
        return None
    try:
        host = urlsplit(text if "://" in text else f"aegis://{text}").hostname
    except ValueError:
        return None
    return (host or "").rstrip(".").lower() or None


def parse_host_list(raw: str) -> tuple[str, ...]:
    """把配置里的主机白名单读成规范化元组：去空白、去重、小写、丢空项。"""
    hosts = [host for host in (normalize_host(item) for item in str(raw or "").split(",")) if host]
    return tuple(dict.fromkeys(hosts))


@dataclass(frozen=True, slots=True)
class OutboundPolicy:
    """外呼策略。`allowed_hosts` 为空即整个能力关闭——没有"默认放开"这种取值。"""

    allowed_hosts: tuple[str, ...] = ()
    timeout_ms: int = 4_000

    @classmethod
    def from_settings(cls, settings: Settings) -> OutboundPolicy:
        return cls(
            allowed_hosts=parse_host_list(settings.workflow_http_allowed_hosts),
            timeout_ms=int(settings.workflow_http_timeout_ms),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.allowed_hosts)


class OutboundCaller:
    """按策略放行的 HTTP 外呼，并把"发了几次、拒了几次、为什么拒"记成可外显的事实。

    客户端可注入（单测打桩）；未注入时第一次外呼才建连接池，`aclose()` 只关自己建的那个。
    """

    def __init__(self, policy: OutboundPolicy, client: Any = None) -> None:
        self._policy = policy
        self._client = client
        self._owns_client = client is None
        self.calls = 0
        self.rejected = 0
        self.failures = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._policy.enabled

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        return self._policy.allowed_hosts

    def refusal_reason(self, method: str, url: str) -> str | None:
        """返回拒绝原因；`None` 表示允许发出。

        单独成一个可调用面，是为了让"为什么这请求没发出去"既测得到也能直接给运维看，
        而不是只能从异常文本里猜。
        """
        if not self._policy.enabled:
            return REASON_DISABLED
        verb = str(method).strip().upper()
        if verb not in ALLOWED_METHODS:
            return f"方法不在允许集合：{verb}"
        try:
            parsed = urlsplit(str(url))
        except ValueError:
            return "URL 无法解析"
        scheme = parsed.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            return f"协议不在允许集合：{scheme or '<none>'}"
        if parsed.username is not None or parsed.password is not None:
            return "URL 里不允许带凭据"
        host = (parsed.hostname or "").rstrip(".").lower()
        if not host:
            return "URL 没有主机名"
        if host not in self._policy.allowed_hosts:
            return f"主机不在白名单：{host}"
        return None

    async def __call__(self, method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        reason = self.refusal_reason(method, url)
        if reason is not None:
            self.rejected += 1
            self.last_error = f"{reason}（目标 {self._safe_target(url)}）"
            raise OutboundTargetError(f"工作流外呼被拒绝：{reason}", detail={"reason": reason})
        client = self._client if self._client is not None else self._new_client()
        try:
            response = await client.request(
                method=str(method).strip().upper(),
                url=str(url),
                json=body,
                timeout=self._policy.timeout_ms / 1000,
                follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            # 计数与上抛同时发生：吞掉它会让"联动设备没收到指令"变成节点输出里一个看不见的空对象。
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            raise
        self.calls += 1
        if isinstance(payload, dict):
            return payload
        # 端点回数组/标量时给一个稳定外壳：下游节点不该因为对方返回形状而拿到忽长忽短的键。
        return {"result": payload}

    def status(self) -> dict[str, Any]:
        """给装配面板的事实：主机白名单是配置项不是凭据，可以原样外显。"""
        return {
            "enabled": self.enabled,
            "allowed_hosts": list(self._policy.allowed_hosts),
            "calls": self.calls,
            "rejected": self.rejected,
            "failures": self.failures,
            "last_error": self.last_error,
            "timeout_ms": self._policy.timeout_ms,
        }

    async def aclose(self) -> None:
        if not self._owns_client or self._client is None:
            return
        client, self._client = self._client, None
        await client.aclose()

    def _new_client(self) -> Any:
        """连接池构造点：测试通过覆盖它换掉真实网络，而不是去改私有字段。"""
        self._client = self._build_client()
        return self._client

    def _build_client(self) -> Any:
        import httpx

        return httpx.AsyncClient()

    @staticmethod
    def _safe_target(url: str) -> str:
        """只留 `scheme://host`：URL 可能带 query token，异常与状态行里都不该出现整条。"""
        try:
            parsed = urlsplit(str(url))
        except ValueError:
            return "<url>"
        if not parsed.scheme or not parsed.netloc:
            return "<url>"
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme.lower()}://{host}{port}"


__all__ = [
    "ALLOWED_METHODS",
    "ALLOWED_SCHEMES",
    "OutboundCaller",
    "OutboundPolicy",
    "OutboundTargetError",
    "parse_host_list",
]
