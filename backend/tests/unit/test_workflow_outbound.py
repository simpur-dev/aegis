"""工作流外呼的策略面：主机白名单是闸，不是装饰。

为什么要有这一层：`api_call`/`device_control` 的目标 URL 写在编排画布上、由流程定义者填，
不设闸等于让平台往任意地址发请求（内网元数据端点只差一次点击）。
2026-10-02 的独立审计同时实测到反方向的问题：装配桥从来没注入 `http_call`，
所以这两类节点在产品形态下 100% 失败。两个方向都得有用例钉着（桥侧见
`test_workflow_services_bridge.py`）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from aegis.config import Settings
from aegis.workflow.outbound import (
    REASON_DISABLED,
    OutboundCaller,
    OutboundPolicy,
    OutboundTargetError,
    parse_host_list,
)


class FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


class FakeClient:
    """只记录"到底发没发出去"：这条腿最容易出的事故就是静默没发或偷偷发了。"""

    def __init__(self, payload: Any = None, *, status: int = 200, error: BaseException | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.closed = 0
        self._payload = {"ok": True} if payload is None else payload
        self._status = status
        self._error = error

    async def request(self, **kwargs: Any) -> FakeResponse:
        if self._error is not None:
            raise self._error
        self.requests.append(kwargs)
        return FakeResponse(self._payload, self._status)

    async def aclose(self) -> None:
        self.closed += 1


def make_caller(hosts: str = "api.example.com", *, client: Any = None, timeout_ms: int = 4_000) -> OutboundCaller:
    policy = OutboundPolicy(allowed_hosts=parse_host_list(hosts), timeout_ms=timeout_ms)
    return OutboundCaller(policy, client=client)


class TestRefusals:
    def test_白名单为空时整条腿关闭且一次请求都不发(self) -> None:
        client = FakeClient()
        caller = make_caller("", client=client)
        assert caller.enabled is False
        assert caller.refusal_reason("GET", "https://api.example.com/x") == REASON_DISABLED

    async def test_关闭时调用是响亮失败而不是返回空对象(self) -> None:
        client = FakeClient()
        caller = make_caller("", client=client)
        with pytest.raises(OutboundTargetError):
            await caller("GET", "https://api.example.com/x")
        assert client.requests == []
        assert (caller.calls, caller.rejected) == (0, 1)

    @pytest.mark.parametrize(
        "url",
        [
            # 真实攻击面样本：云厂商元数据端点、内网环回、协议走私。
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:6379/",
            "file:///etc/passwd",
            "gopher://api.example.com/",
            "//api.example.com/path",
        ],
    )
    def test_非白名单或非_http_的目标一律拒(self, url: str) -> None:
        assert make_caller().refusal_reason("GET", url) is not None

    def test_白名单命中才放行(self) -> None:
        assert make_caller("api.example.com,edge.internal").refusal_reason("POST", "https://edge.internal/c") is None

    def test_主机匹配忽略大小写尾部点与端口(self) -> None:
        # 白名单只比主机名（不比端口、不比路径）——这条要写清，别让人以为端口也管住了。
        caller = make_caller("API.Example.com.")
        assert caller.refusal_reason("GET", "https://api.example.com:8443/p?q=1") is None

    def test_带凭据的URL一律拒(self) -> None:
        reason = make_caller().refusal_reason("GET", "http://user:tok***@api.example.com/x")
        assert reason is not None and "凭据" in reason

    def test_方法不在允许集合就拒(self) -> None:
        assert make_caller().refusal_reason("TRACE", "https://api.example.com/x") is not None
        assert make_caller().refusal_reason(" post ", "https://api.example.com/x") is None


class TestDispatch:
    async def test_放行时不跟随重定向并带上配置的超时(self) -> None:
        client = FakeClient()
        caller = make_caller(client=client, timeout_ms=1_500)
        await caller("post", "https://api.example.com/cmd", {"x": 1})
        sent = client.requests[0]
        assert sent["method"] == "POST"
        assert sent["follow_redirects"] is False
        assert sent["timeout"] == pytest.approx(1.5)
        assert sent["json"] == {"x": 1}
        assert caller.calls == 1

    async def test_对方回数组或标量时给一个稳定外壳(self) -> None:
        caller = make_caller(client=FakeClient([1, 2]))
        assert await caller("GET", "https://api.example.com/list") == {"result": [1, 2]}

    async def test_失败既不吞也只留主机不露完整URL(self) -> None:
        client = FakeClient(error=RuntimeError("connection refused"))
        caller = make_caller(client=client)
        with pytest.raises(RuntimeError):
            await caller("GET", "https://api.example.com/p?token=***")
        assert (caller.calls, caller.failures) == (0, 1)
        assert "TOKEN-CONFIDENTIAL" not in str(caller.last_error)

    async def test_状态面里不得出现凭据或完整URL(self) -> None:
        caller = make_caller(client=FakeClient(error=RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            await caller("GET", "https://api.example.com/p?token=***")
        dumped = json.dumps(caller.status(), ensure_ascii=False)
        assert "TOKEN-CONFIDENTIAL" not in dumped
        assert "/p?token=" not in dumped
        assert caller.status()["allowed_hosts"] == ["api.example.com"]

    async def test_注入的客户端不归自己关(self) -> None:
        client = FakeClient()
        caller = make_caller(client=client)
        await caller.aclose()
        assert client.closed == 0

    async def test_自建客户端是懒建且自己关得掉(self, monkeypatch: pytest.MonkeyPatch) -> None:
        built: list[FakeClient] = []

        def build(self: OutboundCaller) -> FakeClient:
            client = FakeClient()
            built.append(client)
            return client

        monkeypatch.setattr(OutboundCaller, "_build_client", build)
        caller = make_caller()
        assert built == []  # 构造阶段不建连接池
        await caller("GET", "https://api.example.com/x")
        assert len(built) == 1
        await caller.aclose()
        assert built[0].closed == 1


class TestPolicyFromSettings:
    def test_配置面两个键真的被读到(self) -> None:
        policy = OutboundPolicy.from_settings(
            Settings(
                env="test",
                workflow_http_allowed_hosts=" A.Example.com , a.example.com ,, edge.internal , ",
                workflow_http_timeout_ms=1_234,
            )
        )
        assert policy.allowed_hosts == ("a.example.com", "edge.internal")
        assert policy.timeout_ms == 1_234
        assert policy.enabled is True

    def test_默认配置就是关闭的(self) -> None:
        # "默认放开"永远不该是一种取值：新装平台不该自带一条任意外呼通道。
        assert OutboundPolicy.from_settings(Settings(env="test")).enabled is False

    def test_白名单解析规则只有一份(self) -> None:
        assert parse_host_list("A.com.,  a.com,b.cn") == ("a.com", "b.cn")
        assert parse_host_list("") == ()

    def test_条目被收敛成主机名_凭据绝不留存(self) -> None:
        """白名单会原样出现在 `GET /api/v1/integrations`（匿名可读），入口就得剥干净。"""
        hosts = parse_host_list("https://API.example.com/v1/, ops:sup3rs3cr3t@b.cn, , @, http://c.cn:8443/x?y=1")

        assert hosts == ("api.example.com", "b.cn", "c.cn")  # 端口也剥掉：白名单是主机粒度的
        assert "sup3rs3cr3t" not in repr(hosts)
        assert not any("@" in host or "/" in host for host in hosts)
