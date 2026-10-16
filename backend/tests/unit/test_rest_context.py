"""REST 取数门面（§3/§6"五智能体 API 版"）：发的是真请求，降级的是真原因，不假造返回体。

用例经可注入的 client 工厂把 `httpx.MockTransport` 换进去（与 `connectors/weather_api.py` 同款
DI），所以请求行、查询参数、鉴权头都是真发出去的；响应体按 `api/routers/knowledge.py` 两个路由的真实形状给回
（`items` / `detail`）。这里能证明"内环确实只经 HTTP 取数、拿不到时如实留白且原因外显"，
不能证明线上延迟与命中率——那属于 MA 出口门禁的现场实测。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from aegis.agents.assess import AssessAgent, AssessContextProvider, AssessVerdict
from aegis.agents.rest_context import CASES_PATH, SEARCH_PATH, RestAssessContext
from aegis.domain.enums import AgentType, HazardType
from aegis.domain.messages import make_request, now_iso

REGION = "540121"
CASE_ITEM = {"title": "2024 河口泥石流", "degraded": False}
DOC_ITEM = {"id": "doc-1", "title": "应急预案第 3 节"}

OK_CASES = (200, {"query": "debris_flow", "count": 1, "degraded_count": 0, "budget_ms": 800, "items": [CASE_ITEM]})
OK_SEARCH = (200, {"query": "debris_flow", "items": [DOC_ITEM]})
EMPTY = (200, {"items": []})


class Probe:
    """记下真发出去的请求，并按路由表给回响应。"""

    def __init__(self, routes: dict[str, tuple[int, Any]], *, fail: Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._routes = routes
        self._fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._fail is not None:
            raise self._fail
        status, body = self._routes[request.url.path]
        return httpx.Response(status, json=body)

    def factory(self, **kwargs: Any) -> httpx.AsyncClient:
        # 生产侧传进来的 transport（真联调可换）在这里被测试替身顶掉，其余参数原样保留。
        kwargs.pop("transport", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), **kwargs)

    def path(self, name: str) -> httpx.Request:
        return next(request for request in self.requests if request.url.path == name)


def subject(routes: dict[str, tuple[int, Any]], *, fail: Exception | None = None, **kw: Any) -> tuple[RestAssessContext, Probe]:
    probe = Probe(routes, fail=fail)
    provider = RestAssessContext(base_url="http://aegis.internal/", client_factory=probe.factory, **kw)
    return provider, probe


async def test_两条腿都有命中时按真实items交出且降级列表为空() -> None:
    provider, _ = subject({CASES_PATH: OK_CASES, SEARCH_PATH: OK_SEARCH})
    ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
    assert ctx["cases"] == [CASE_ITEM] and ctx["retrieval"] == [DOC_ITEM]
    assert ctx["degraded"] == []


async def test_请求行带齐路由声明的查询参数与令牌头() -> None:
    provider, probe = subject({CASES_PATH: EMPTY, SEARCH_PATH: EMPTY}, case_limit=4, search_k=7, token="s3cret")
    await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
    cases = probe.path(CASES_PATH)
    search = probe.path(SEARCH_PATH)
    assert dict(cases.url.params) == {"region_code": REGION, "q": "debris_flow", "hazard_type": "debris_flow", "limit": "4"}
    assert dict(search.url.params) == {"region_code": REGION, "q": "debris_flow", "k": "7"}, "检索路由不接 hazard_type，硬塞会 422"
    assert cases.headers["authorization"] == "Bearer s3cret"
    assert cases.url.host == "aegis.internal", "base_url 尾斜杠不能把路径拼成 //"


async def test_未配令牌时不发authorization头() -> None:
    provider, probe = subject({CASES_PATH: EMPTY, SEARCH_PATH: EMPTY})
    await provider.corroborate(region_code=REGION, hazard_hint=None, hits=[])
    assert all("authorization" not in request.headers for request in probe.requests)


async def test_五十3时把接口原话搬进降级原因且不塞空命中() -> None:
    """变异点：把 503 也读成"没有案例"，这一腿就静默缺键，下游分不出"没装配"与"确实没相似案例"。"""
    provider, _ = subject({CASES_PATH: (503, {"detail": "知识层未装配"}), SEARCH_PATH: OK_SEARCH})
    ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
    assert "cases" not in ctx
    assert ctx["degraded"] == ["cases: 知识层未装配"]
    assert ctx["retrieval"] == [DOC_ITEM], "一条腿挂了不拖累另一条腿"


async def test_二十零但确无命中时既不留空列表也不记降级() -> None:
    provider, _ = subject({CASES_PATH: EMPTY, SEARCH_PATH: EMPTY})
    ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
    assert "cases" not in ctx and "retrieval" not in ctx
    assert ctx["degraded"] == [], "查过了、就是没有：这不是故障"


async def test_传输层异常不抛出只记原因() -> None:
    provider, probe = subject({CASES_PATH: EMPTY, SEARCH_PATH: EMPTY}, fail=httpx.ConnectError("connection refused"))
    ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
    assert len(probe.requests) == 2, "两条腿都真发过请求"
    assert ctx["degraded"] == [
        "cases: ConnectError: connection refused",
        "retrieval: ConnectError: connection refused",
    ]


async def test_超时归成请求超时而不是异常上抛() -> None:
    provider, _ = subject({CASES_PATH: EMPTY, SEARCH_PATH: EMPTY}, fail=httpx.ReadTimeout("too slow"))
    ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
    assert ctx["degraded"] == ["cases: 请求超时", "retrieval: 请求超时"]


async def test_非枚举灾种不发hazard_type参数() -> None:
    """接口的 hazard_type 是枚举查询参数，自由文本会换来 422；宁可只发 q，也不发一个会被拒的参数。"""
    provider, probe = subject({CASES_PATH: EMPTY, SEARCH_PATH: EMPTY})
    await provider.corroborate(region_code=REGION, hazard_hint="泥石流", hits=[])
    cases = probe.path(CASES_PATH)
    assert "hazard_type" not in cases.url.params and cases.url.params["q"] == "泥石流"


async def test_灾种可以从hits里推出来() -> None:
    provider, probe = subject({CASES_PATH: EMPTY, SEARCH_PATH: EMPTY})
    hits = [{"hazard_type": HazardType.LANDSLIDE.value, "rule_id": "R-1"}]
    await provider.corroborate(region_code=REGION, hazard_hint=None, hits=hits)
    cases = probe.path(CASES_PATH)
    assert cases.url.params["hazard_type"] == "landslide" and cases.url.params["q"] == "landslide"


async def test_非2xx状态码记成HTTP状态() -> None:
    provider, _ = subject({CASES_PATH: (401, {"detail": "unauthorized"}), SEARCH_PATH: EMPTY})
    ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
    assert ctx["degraded"] == ["cases: HTTP 401"]


async def test_它是AssessContextProvider要的那个形状() -> None:
    provider, _ = subject({CASES_PATH: EMPTY, SEARCH_PATH: EMPTY})
    assert isinstance(provider, AssessContextProvider), "装配点按协议注入，形状不认就是取数断链"


def _request(hits: list[Any] | None = None) -> Any:
    return make_request(
        source="platform.t01",
        target="assess.t01",
        action="assess.risk",
        payload={"region_code": REGION, "hits": hits or [], "observed_at": now_iso()},
        trace_id="trc_" + "0" * 16,
        reply_to="reply.t01.inbox",
        deadline_ms=3000,
    )


class FakeGateway:
    def __init__(self) -> None:
        self.prompt_vars: list[dict[str, Any]] = []

    async def chat_structured(self, messages: list[dict[str, str]], **kwargs: Any) -> AssessVerdict:
        self.prompt_vars.append(kwargs.get("prompt_vars") or {})
        return AssessVerdict(risk_level=3, hazard_type="debris_flow", confidence=0.7, rationale="强降雨", evidence_refs=["R-DEBRIS-RAIN-1"])


def assess(context: Any, gateway: FakeGateway) -> AssessAgent:
    return AssessAgent(
        transport=None,
        agent_type=AgentType.ASSESS,
        agent_id="assess.t01",
        capabilities=["risk_assess"],
        llm=gateway,
        context=context,
        double_blind=False,
    )  # type: ignore[arg-type]


async def test_assess把取数降级原样带到响应里() -> None:
    """响应必须看得见"这条结论没有佐证"——只看定级数字会读成佐证就是那些 items。"""

    class BrokenContext:
        async def corroborate(self, *, region_code: str, hazard_hint: str | None, hits: list[Any]) -> dict[str, Any]:
            return {"degraded": ["cases: 请求超时"]}

    gateway = FakeGateway()
    reply = await assess(BrokenContext(), gateway).handle(_request())
    assert reply.payload["context_degraded"] == ["cases: 请求超时"]


async def test_assess取数正常时响应里降级为空() -> None:
    provider, _ = subject({CASES_PATH: OK_CASES, SEARCH_PATH: OK_SEARCH})
    gateway = FakeGateway()
    # 带 hit 才有可搜的词：`hits=[]` 那档检索腿根本不问（见 `test_没有关键词时检索腿不发`）
    reply = await assess(provider, gateway).handle(_request(hits=[{"hazard_type": "debris_flow", "rule_id": "R-DEBRIS-RAIN-1"}]))
    assert reply.payload["context_degraded"] == []
    joined = gateway.prompt_vars[0]["context"]
    assert "2024 河口泥石流" in joined and "应急预案第 3 节" in joined, "取到的佐证必须真的进提示词，否则取数白取"
    assert "AEGIS_DATA" in joined, "外部佐证进提示词前必须过注入围栏"


@pytest.mark.parametrize("status", [500, 502])
async def test_服务端错误不会把上下文变成异常上抛(status: int) -> None:
    provider, _ = subject({CASES_PATH: (status, {"detail": "boom"}), SEARCH_PATH: EMPTY})
    ctx = await provider.corroborate(region_code=REGION, hazard_hint="debris_flow", hits=[])
    assert ctx["degraded"] == [f"cases: HTTP {status}"]


async def test_没有关键词时检索腿不发() -> None:
    """`q` 是 `/api/v1/retrieval/search` 的必填非空参数：没词硬发只换来一个 422。

    原来这条会被记成 `retrieval: HTTP 422` 进 `context_degraded`——把"这次没有可搜的词"说成故障，
    而人工上报未越限那类事件本来就没词，清单会被刷成噪声。现在不发也不记。
    """
    provider, probe = subject({CASES_PATH: OK_CASES, SEARCH_PATH: OK_SEARCH})
    ctx = await provider.corroborate(region_code=REGION, hazard_hint=None, hits=[])
    assert [request.url.path for request in probe.requests] == [CASES_PATH]
    assert ctx["cases"] == [CASE_ITEM] and "retrieval" not in ctx and ctx["degraded"] == []
