"""外部通知通道（HTTP webhook）：把站内那一条送到平台之外（设计 §14.2）。

为什么单独一层，而不是复用 `workflow/outbound.py` 的 `OutboundCaller`：
两条腿的**授权边界不同**——工作流外呼的目标 URL 由**流程定义者**在画布上填，
通知的目标由**部署方**在配置里定。共用一条白名单等于把"编辑画布的人"的权限
扩大到"能把平台的通知发到任意主机"。所以这里只复用那两个**纯函数**
（`parse_host_list` / `normalize_host`，保证主机规范化口径是同一份），
策略对象与计数器另起一套：工作流的 calls/rejected/failures 与通知通道不是一回事，
混在一起就分不清"是工作流被拒了还是通知被拒了"。

三条口径：
1. **白名单空 = 通道关闭**（没有"默认放开"这种取值）；
2. **不跟随重定向**：一个白名单主机不能把请求转到任意内网地址；
3. **外发失败不抛给调用方**：站内是真源、外发只是加速器，失败要**记**而不是**炸**——
   炸了会让"通知已落站内"这件事看起来像失败，值班员于是重发一遍。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from aegis.workflow.outbound import normalize_host, parse_host_list

#: 外发载荷的字段白名单：**只发通知本身**，不发收件人的凭据、不发审计原文。
PAYLOAD_FIELDS = ("recipient", "message", "kind", "sender", "space_id", "created_at")

REASON_DISABLED = "外部通知未启用：没有配置接收端或白名单为空"


@dataclass(frozen=True, slots=True)
class WebhookPolicy:
    """外部通知的策略。`allowed_hosts` 为空即整条通道关闭。"""

    url: str = ""
    allowed_hosts: tuple[str, ...] = ()
    timeout_ms: int = 4_000

    @classmethod
    def from_settings(cls, settings: Any) -> WebhookPolicy:
        return cls(
            url=str(getattr(settings, "notification_webhook_url", "") or "").strip(),
            allowed_hosts=parse_host_list(getattr(settings, "notification_webhook_hosts", "") or ""),
            timeout_ms=int(getattr(settings, "notification_webhook_timeout_ms", 4000) or 4000),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url) and bool(self.allowed_hosts)

    def refusal_reason(self) -> str | None:
        """为什么发不出去。`None` 表示允许发出。"""
        if not self.url:
            return "外部通知未启用：没有配置 AEGIS_NOTIFICATION_WEBHOOK_URL"
        if not self.allowed_hosts:
            return REASON_DISABLED
        try:
            parsed = urlsplit(self.url)
        except ValueError:
            return "接收端 URL 无法解析"
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            return f"协议不在允许集合：{scheme or '<none>'}"
        if parsed.username is not None or parsed.password is not None:
            return "接收端 URL 里不允许带凭据"
        host = normalize_host(self.url)
        if not host:
            return "接收端 URL 没有主机名"
        if host not in self.allowed_hosts:
            return f"主机不在白名单：{host}"
        return None


class NotificationWebhook:
    """按白名单放行的通知外发。

    客户端可注入（单测打桩）；未注入时第一次外呼才建连接池。
    """

    def __init__(self, policy: WebhookPolicy, client: Any = None) -> None:
        self._policy = policy
        self._client = client
        self._owns_client = client is None
        self.calls = 0
        self.failures = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._policy.enabled

    @property
    def policy(self) -> WebhookPolicy:
        return self._policy

    async def deliver(self, record: dict[str, Any]) -> dict[str, Any]:
        """把一条站内通知发到外部通道。

        **永远不抛异常**：返回 `{delivered: bool, status: str, error: str | None}`。
        调用方据此写 `delivery_status`（`delivered` / `failed`），而站内那一条照样成立。
        """
        reason = self._policy.refusal_reason()
        if reason is not None:
            self.failures += 1
            self.last_error = reason
            return {"delivered": False, "status": "failed", "error": reason}
        payload = {field: record.get(field) for field in PAYLOAD_FIELDS}
        client = self._client if self._client is not None else self._new_client()
        try:
            response = await client.post(
                self._policy.url,
                json=payload,
                timeout=self._policy.timeout_ms / 1000,
                follow_redirects=False,
            )
            response.raise_for_status()
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            return {"delivered": False, "status": "failed", "error": self.last_error}
        self.calls += 1
        return {"delivered": True, "status": "delivered", "error": None}

    def status(self) -> dict[str, Any]:
        """给装配面板的事实：白名单是配置项不是凭据，可以原样外显。"""
        return {
            "enabled": self.enabled,
            "url": self._safe_target(self._policy.url),
            "allowed_hosts": list(self._policy.allowed_hosts),
            "calls": self.calls,
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
        self._client = self._build_client()
        return self._client

    def _build_client(self) -> Any:
        import httpx

        return httpx.AsyncClient()

    @staticmethod
    def _safe_target(url: str) -> str:
        """只留 `scheme://host`：URL 可能带 query token，状态行里不该出现整条。"""
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
    "PAYLOAD_FIELDS",
    "REASON_DISABLED",
    "NotificationWebhook",
    "WebhookPolicy",
]
