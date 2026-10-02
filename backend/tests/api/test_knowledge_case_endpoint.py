"""案例入库端点：图谱写入侧必须有生产调用方。

审计实测（2026-10-02）：`GraphitiKnowledgeProvider.learn()` 与 `prepare_schema()` 只被
provider 自己和用例调用——一套新部署的图是空的，每次召回都落到进程内预案库。
"案例时序知识"这条考核口径因此只有读路径的证据。

这里补的是**入库路径**（`POST /api/v1/knowledge/cases`），并守住四条：
① 写进去到底落在哪一侧（图谱 / 仅进程内）必须回在响应里，不能只说"成功"；
② 图谱不可用时案例仍进进程内库，但响应要带着降级原因（不静默、不 500）；
③ 一条 add_episode ≈ 4—7 次 LLM 调用，所以写入只在显式入库/复盘路径上发生，
   不挂进预警热路径（用路由清单把这件事钉住）；
④ 不合法的案例必须被契约挡在门外，既不进图也不进内存库。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container
from aegis.knowledge.cases import HazardCase
from aegis.knowledge.provider import LearnOutcome

BASE = "http://testserver"
RECALL = "/api/v1/cases/recall"

CASE: dict[str, Any] = {
    "case_id": "case_gate_01",
    "title": "沟口泥石流夜间转移演练案例",
    "hazard_types": ["debris_flow"],
    "category": "evacuate",
    "phase": "response",
    "why_now": "10 分钟雨量连续超阈值且裂隙宽度在扩张",
    "target_groups": ["沟口两户牧民", "施工营地"],
    "actions": ["电话 + 北斗短报文双通道通知", "预置撤收点照明"],
    "estimated_delay_hours": 1.5,
    "confidence": 0.7,
    "region_prefixes": ["5401"],
    "applies_to_levels": [2, 3],
    "source_note": "门禁样例（非现场复盘）",
}


def base_settings() -> Settings:
    return Settings(env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False)


async def _client(container: PlatformContainer) -> httpx.AsyncClient:
    app = create_app(container.settings, container=container)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


@pytest.fixture
async def container() -> AsyncIterator[PlatformContainer]:
    ctn = create_container(base_settings(), with_simulator=False)
    yield ctn
    await ctn.shutdown()


@pytest.fixture
async def client(container: PlatformContainer) -> AsyncIterator[httpx.AsyncClient]:
    http = await _client(container)
    yield http
    await http.aclose()


async def _recall_ids(http: httpx.AsyncClient, query: str = "沟口 泥石流") -> set[str]:
    body = (await http.get(RECALL, params={"q": query, "limit": 20})).json()
    return {str(item["case_id"]) for item in body["items"]}


class TestEndpoint:
    async def test_入库后端点回答驱动与降级事实(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/api/v1/knowledge/cases", json=CASE)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["case_id"] == "case_gate_01"
        assert body["driver"] in {"graphiti", "in_memory"}
        assert isinstance(body["degraded"], bool)
        assert body["title"] == CASE["title"]

    async def test_纯内存装配要把自己报成in_memory而不是假装图谱(self, client: httpx.AsyncClient) -> None:
        # 这套测试装配没有图谱 URI，所以"驱动=graphiti"就是谎报——它必须能被端点层面看到。
        body = (await client.post("/api/v1/knowledge/cases", json=CASE)).json()
        assert (body["driver"], body["degraded"], body["reason"]) == ("in_memory", False, "")

    async def test_写进去的案例立刻能被召回_证明不是只回了一句成功(self, client: httpx.AsyncClient) -> None:
        assert (await client.post("/api/v1/knowledge/cases", json=CASE)).status_code == 200
        assert "case_gate_01" in await _recall_ids(client)

    async def test_同一条案例重复入库是覆盖不是新增(self, client: httpx.AsyncClient) -> None:
        revised = {**CASE, "confidence": 0.93}
        assert (await client.post("/api/v1/knowledge/cases", json=CASE)).status_code == 200
        assert (await client.post("/api/v1/knowledge/cases", json=revised)).status_code == 200
        body = (await client.get(RECALL, params={"q": "沟口", "limit": 20})).json()
        mine = [item for item in body["items"] if item["case_id"] == "case_gate_01"]
        assert len(mine) == 1
        assert mine[0]["confidence"] == 0.93

    @pytest.mark.parametrize(
        "patch",
        [
            {"case_id": "BAD"},  # 编号不合约定
            {"case_id": "case_gate_02", "title": "短"},  # 标题过短
            {"case_id": "case_gate_03", "confidence": 1.5},  # 置信度越界
            {"case_id": "case_gate_04", "actions": []},  # 没有可执行动作
            {"case_id": "case_gate_05", "hazard_types": ["Debris Flow"]},  # 灾种 token 不合法
            {"case_id": "case_gate_06", "出处": "中文键"},  # extra=forbid
            {"case_id": "case_gate_07", "category": "response"},  # 类别不在词表
            {"case_id": "case_gate_08", "region_prefixes": ["54010000000000"]},  # 区划前缀过长
        ],
    )
    async def test_不合契约的案例直接422_不进图谱也不进内存库(self, client: httpx.AsyncClient, patch: dict[str, Any]) -> None:
        payload = {**CASE, **patch}
        assert (await client.post("/api/v1/knowledge/cases", json=payload)).status_code == 422
        assert patch.get("case_id", "case_gate_01") not in await _recall_ids(client)

    async def test_知识层缺席时是503并且说清缺什么(self) -> None:
        ctn = create_container(base_settings(), with_simulator=False)
        ctn.knowledge = None  # type: ignore[assignment]
        http = await _client(ctn)
        try:
            response = await http.post("/api/v1/knowledge/cases", json=CASE)
        finally:
            await http.aclose()
            await ctn.shutdown()
        assert response.status_code == 503
        assert "knowledge" in str(response.json()["detail"])

    def test_这条写路径是唯一的知识层写入口(self) -> None:
        # 一次 add_episode ≈ 4—7 次 LLM 调用：热路径上悄悄写图谱是本模块要防的事，
        # 所以知识层只允许这一个 POST 入口。
        container = create_container(base_settings(), with_simulator=False)
        app = create_app(container.settings, container=container)
        posts = {route.path for route in app.routes if "POST" in (getattr(route, "methods", None) or set()) and "/knowledge" in route.path}
        assert posts == {"/api/v1/knowledge/cases"}

    async def test_召回端点只读_一个字节都不写图谱(self, container: PlatformContainer) -> None:
        """P0 硬约束在 HTTP 面上的落点：读端点不许顺带触发 `learn`。

        写一次图谱 ≈ 4—7 次 LLM 调用。这件事一旦发生在召回里，预警链路的 ≤3min 预算
        就被一次查案例买走了，而它在时延报表里只会显示成"召回很慢"。
        """
        counter = RecordingProvider()
        container.knowledge = counter

        http = await _client(container)
        try:
            assert (await http.get(RECALL, params={"q": "沟口"})).status_code == 200
            assert (await http.post("/api/v1/knowledge/cases", json=CASE)).status_code == 200
        finally:
            await http.aclose()

        assert counter.reads == 1
        assert counter.writes == 1


class RecordingProvider:
    """只记账的知识层替身：把"读路径有没有写"变成可断言的计数。"""

    driver = "in_memory"

    def __init__(self) -> None:
        self.reads = 0
        self.writes = 0

    async def recall(self, query: str, **_: Any) -> list[Any]:
        self.reads += 1
        return []

    async def learn(self, case: HazardCase) -> None:
        self.writes += 1

    async def learn_case(self, case: HazardCase) -> LearnOutcome:
        self.writes += 1
        return LearnOutcome(case_id=case.case_id, driver=self.driver)
