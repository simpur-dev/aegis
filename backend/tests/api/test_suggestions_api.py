"""阈值建议三个端点的 HTTP 侧判据（装配级：`store_backend=memory`）。

能证的与不能证的要分清。内存读视图没有建议台账，所以三条端点**必须** 503 并说清缺什么——
这本身就是判据（"收下建议但库里没这张表"是最坏的假成功：值班员看见"已确认"，判据一个字没动）。
至于确认后规则库真长出新版本、引擎真换上，是 `tests/integration/test_suggestions_live.py`
在真 Postgres 上量的事。载荷形状不需要库：FastAPI 在进处理函数之前就按模型校验，422 当场可断。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container

LIST_PATH = "/api/v1/feedback/suggestions"
CONFIRM_PATH = "/api/v1/feedback/suggestions/sug_aaaabbbbcccc/confirm"
REJECT_PATH = "/api/v1/feedback/suggestions/sug_aaaabbbbcccc/reject"

_CONFIRM_BODY = {
    "rule_id": "R-DEBRIS-RAIN-1",
    "metric": "rain_10min",
    "threshold": 25.0,
    "expected_version": 1,
    "decided_by": "王工",
    "note": "现场复核 6 月两次空报",
}
_REJECT_BODY = {"decided_by": "王工", "note": "样本太少，不认"}


@pytest.fixture()
def settings() -> Settings:
    return Settings(env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, delivery_mode="mock", llm_api_key="")


@asynccontextmanager
async def _client(container: PlatformContainer) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container.settings, container=container)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


async def _container(settings: Settings) -> PlatformContainer:
    container = create_container(settings, with_simulator=False)
    await container.start()
    return container


class Test缺台账时三条端点的说法:
    async def test_清单确认驳回都如实503并写清要哪条腿(self, settings: Settings) -> None:
        container = await _container(settings)
        try:
            async with _client(container) as c:
                responses = [
                    await c.get(LIST_PATH),
                    await c.post(CONFIRM_PATH, json=_CONFIRM_BODY),
                    await c.post(REJECT_PATH, json=_REJECT_BODY),
                ]
            for response in responses:
                assert response.status_code == 503, response.text
                detail = response.json()["detail"]
                assert detail["code"] == "E_SUGGESTION_LEDGER_UNAVAILABLE", detail
                assert "postgres" in detail["requires"], "503 要说清缺的是哪条腿，别让运维去查网络"
        finally:
            await container.shutdown()

    async def test_装配事实本身也说这一条(self, settings: Settings) -> None:
        """缺的腿要在"这台装了什么"里看得见，而不是等有人来按确认才知道。"""
        container = await _container(settings)
        try:
            assert container.suggestions is not None and not container.suggestions.available
            assert "台账" in container.suggestions.unavailable_reason
        finally:
            await container.shutdown()


class Test载荷形状:
    """这几条不需要库：校验发生在进处理函数之前，所以 422 与 503 的先后本身就是判据。"""

    async def test_缺签字人就是422而不是503(self, settings: Settings) -> None:
        """参数不合法不该被说成"服务不可用"：那是让人去查数据库连接。"""
        container = await _container(settings)
        try:
            async with _client(container) as c:
                body = dict(_CONFIRM_BODY)
                body.pop("decided_by")
                assert (await c.post(CONFIRM_PATH, json=body)).status_code == 422
        finally:
            await container.shutdown()

    async def test_不许塞绕过人工的字段(self, settings: Settings) -> None:
        # `extra="forbid"` 的用处：防止将来有人加一个 auto_apply=true 把自己免掉
        container = await _container(settings)
        try:
            async with _client(container) as c:
                assert (await c.post(CONFIRM_PATH, json={**_CONFIRM_BODY, "auto_apply": True})).status_code == 422
        finally:
            await container.shutdown()

    async def test_阈值必须是数且不为负(self, settings: Settings) -> None:
        container = await _container(settings)
        try:
            async with _client(container) as c:
                for bad in (-1.0, "三十"):
                    assert (await c.post(CONFIRM_PATH, json={**_CONFIRM_BODY, "threshold": bad})).status_code == 422, bad
        finally:
            await container.shutdown()

    async def test_对照版本必填(self, settings: Settings) -> None:
        """没有 `expected_version` 的确认，等于允许"凭三周前看到的那一屏"覆盖现在的判据。"""
        container = await _container(settings)
        try:
            async with _client(container) as c:
                body = dict(_CONFIRM_BODY)
                body.pop("expected_version")
                assert (await c.post(CONFIRM_PATH, json=body)).status_code == 422
        finally:
            await container.shutdown()

    async def test_清单的状态过滤器只认库里那三个值加all(self, settings: Settings) -> None:
        container = await _container(settings)
        try:
            async with _client(container) as c:
                assert (await c.get(LIST_PATH, params={"status": "编不出来的状态"})).status_code == 422
                assert (await c.get(LIST_PATH, params={"status": "confirmed"})).status_code == 503
        finally:
            await container.shutdown()


class Test建议端点也在服务令牌闸里:
    async def test_配了令牌时不带凭据一律401(self) -> None:
        """新增端点默认被闸覆盖——这正是上一批把鉴权收成一条中间件想要的效果。"""
        cfg = Settings(
            env="test", bus_backend="memory", store_backend="memory", simulator_enabled=False, delivery_mode="mock", service_token="s3cr3t"
        )
        container = create_container(cfg, with_simulator=False)
        await container.start()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(cfg, container=container)), base_url="http://testserver"
            ) as c:
                assert (await c.get(LIST_PATH)).status_code == 401
                assert (await c.post(CONFIRM_PATH, json=_CONFIRM_BODY)).status_code == 401
                assert (await c.post(REJECT_PATH, json=_REJECT_BODY)).status_code == 401
        finally:
            await container.shutdown()
