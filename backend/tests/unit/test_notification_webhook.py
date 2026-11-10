"""外部通知通道的判据（P4，设计 §14.2）。

钉三件事：
1. **白名单空 = 通道关闭**（没有"默认放开"这种取值）；
2. **站内先行**：站内写失败就不该再发外发（那会造出"外面收到了、平台里查不到"的幽灵通知）；
3. **外发失败不装送达**：失败只写 `delivery_status='failed'`，站内那一条照样成立、时间戳不清空。
"""

from __future__ import annotations

import contextlib
from typing import Any

from aegis.services.alerting.notifier import Notifier
from aegis.services.alerting.webhook import NotificationWebhook, WebhookPolicy
from aegis.storage.store import NotificationStore


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """记录发了什么；`fail=True` 时模拟网络错误。"""

    def __init__(self, *, fail: bool = False, status: int = 200) -> None:
        self.fail = fail
        self.status = status
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if self.fail:
            raise RuntimeError("connection refused")
        return _FakeResponse(self.status)


class _BrokenLedger(NotificationStore):
    """站内写失败：用来钉"站内先行"——站内都没落，就不该发外发。"""

    async def insert_notification(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("站内台账写失败")


def _webhook(client: _FakeClient, *, url: str = "https://hooks.example.com/aegis", hosts: str = "hooks.example.com") -> NotificationWebhook:
    return NotificationWebhook(WebhookPolicy(url=url, allowed_hosts=(hosts,)), client=client)


class Test白名单关闭即不发:
    def test_白名单空时通道关闭(self) -> None:
        policy = WebhookPolicy(url="https://hooks.example.com/x", allowed_hosts=())
        assert policy.enabled is False
        assert policy.refusal_reason() is not None

    def test_没配接收端时通道关闭(self) -> None:
        policy = WebhookPolicy(url="", allowed_hosts=("hooks.example.com",))
        assert policy.enabled is False

    def test_主机不在白名单时拒(self) -> None:
        policy = WebhookPolicy(url="https://evil.example.com/x", allowed_hosts=("hooks.example.com",))
        assert policy.refusal_reason() is not None

    def test_白名单命中时放行(self) -> None:
        policy = WebhookPolicy(url="https://hooks.example.com/x", allowed_hosts=("hooks.example.com",))
        assert policy.refusal_reason() is None

    async def test_通道关闭时投递返回失败而不是抛错(self) -> None:
        client = _FakeClient()
        webhook = NotificationWebhook(WebhookPolicy(url="", allowed_hosts=()), client=client)
        outcome = await webhook.deliver({"id": 1, "message": "x"})
        assert outcome["delivered"] is False
        assert outcome["status"] == "failed"
        assert client.calls == []


class Test站内先行:
    async def test_站内写失败时不发外发(self) -> None:
        """站内是真源：它都没落，就不该发外发（否则外面收到了、平台里查不到）。"""
        client = _FakeClient()
        notifier = Notifier(
            _BrokenLedger(),
            publish=None,
            subject="ops.assistant.notify",
            operators=None,
            webhook=_webhook(client),
        )
        with contextlib.suppress(RuntimeError):
            await notifier.notify(recipient="lead01", message="x", kind="direct")
        assert client.calls == [], "站内都没落成，外发不该发生"

    async def test_外发成功时状态是_delivered(self) -> None:
        ledger = NotificationStore()
        client = _FakeClient()
        notifier = Notifier(ledger, publish=None, subject="ops.assistant.notify", operators=None, webhook=_webhook(client))

        record = await notifier.notify(recipient="lead01", message="x", kind="direct")
        assert record["delivery_status"] == "delivered"
        assert record["delivery_error"] is None
        assert len(client.calls) == 1

    async def test_没有通道时默认_in_app(self) -> None:
        ledger = NotificationStore()
        notifier = Notifier(ledger, publish=None, subject="ops.assistant.notify", operators=None)
        record = await notifier.notify(recipient="lead01", message="x", kind="direct")
        assert record["delivery_status"] == "in_app"


class Test外发失败不装送达:
    async def test_外发失败时站内那一条仍然成立(self) -> None:
        ledger = NotificationStore()
        notifier = Notifier(
            ledger,
            publish=None,
            subject="ops.assistant.notify",
            operators=None,
            webhook=_webhook(_FakeClient(fail=True)),
        )
        record = await notifier.notify(recipient="lead01", message="x", kind="direct")

        assert record["delivery_status"] == "failed"
        assert record["delivery_error"] is not None
        # 站内送达时刻没有被清空：站内是真源，外发只是加速器
        assert record["created_at"] is not None
        stored = await ledger.list_notifications(recipient="lead01")
        assert len(stored) == 1
        assert stored[0]["message"] == "x"

    async def test_外发失败不抛异常(self) -> None:
        """炸了会让"站内已落"看起来像失败，值班员于是重发一遍。"""
        ledger = NotificationStore()
        notifier = Notifier(
            ledger,
            publish=None,
            subject="ops.assistant.notify",
            operators=None,
            webhook=_webhook(_FakeClient(status=500)),
        )
        record = await notifier.notify(recipient="lead01", message="x", kind="direct")
        assert record["delivery_status"] == "failed"

    async def test_外发载荷只带通知本身(self) -> None:
        """不发收件人的凭据、不发审计原文。"""
        ledger = NotificationStore()
        client = _FakeClient()
        notifier = Notifier(ledger, publish=None, subject="ops.assistant.notify", operators=None, webhook=_webhook(client))
        await notifier.notify(recipient="lead01", message="沟道泥位抬升", kind="direct", sender="duty01")

        payload = client.calls[0]["json"]
        assert payload["message"] == "沟道泥位抬升"
        assert "token" not in str(payload).lower()


class Test广播失败不影响站内:
    async def test_广播失败不抛且站内只一条(self) -> None:
        """总线只是加速器：publish 炸了，站内已落就不该抛错，否则值班员重发会造出重复通知。"""
        ledger = NotificationStore()

        async def broken_publish(subject: str, event: Any) -> None:
            raise RuntimeError("bus down")

        notifier = Notifier(ledger, publish=broken_publish, subject="ops.assistant.notify", operators=None)
        record = await notifier.notify(recipient="lead01", message="x", kind="direct")

        stored = await ledger.list_notifications(recipient="lead01")
        assert len(stored) == 1, "广播失败不该让站内通知作废，也不该逼出重发"
        assert record["id"] == stored[0]["id"]
