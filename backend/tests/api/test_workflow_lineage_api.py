"""草稿/版本/发布这一层 HTTP 端点：状态码、错误体与"存进去读回来一致"。

这一层要钉的与单元测试不同：应用服务自己保证的不变量，经过路由、请求模型和异常处理器后
是否仍是同一个事实。尤其是 409 的四种冲突（revision/name/no_changes/archived）必须各自可分辨，
否则前端只能把"有人刚保存过"和"内容没变"都显示成同一句"保存失败"。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container


def _settings() -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=False,
        delivery_mode="mock",
        llm_api_key="",
    )


@asynccontextmanager
async def _client(container: PlatformContainer) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(container.settings, container=container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture()
async def container() -> AsyncIterator[PlatformContainer]:
    ctn = create_container(_settings(), with_simulator=False)
    await ctn.start()
    try:
        yield ctn
    finally:
        await ctn.shutdown()


async def _new_draft(client: httpx.AsyncClient, name: str) -> dict:
    response = await client.post("/api/v1/workflow/lineages", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def _publish(client: httpx.AsyncClient, lineage_id: str, revision: int) -> dict:
    response = await client.post(
        f"/api/v1/workflow/lineages/{lineage_id}/publish",
        json={"base_revision": revision},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _error(response: httpx.Response) -> dict:
    body = response.json()
    assert set(body) == {"detail"}, body
    return body["detail"]


class TestDraftRoundTrip:
    async def test_新建草稿_读取_写入_发布_版本文档(self, container: PlatformContainer) -> None:
        async with _client(container) as client:
            draft = await _new_draft(client, "API 生命周期")
            lineage = draft["lineage_id"]
            assert draft["revision"] == 1
            assert draft["base_workflow_id"] is None
            assert [node["type"] for node in draft["document"]["nodes"]] == ["start"]
            assert draft["document"]["schema_version"] == 1
            assert draft["document"]["layout"]["positions"] == {}

            loaded = await client.get(f"/api/v1/workflow/lineages/{lineage}/draft")
            assert loaded.status_code == 200
            assert loaded.json()["document"] == draft["document"]

            document = loaded.json()["document"]
            document["description"] = "第一版说明"
            document["layout"]["positions"] = {"start": {"x": 120, "y": 80}}
            put = await client.put(
                f"/api/v1/workflow/lineages/{lineage}/draft",
                json={"base_revision": 1, "document": document},
            )
            assert put.status_code == 200, put.text
            assert put.json()["revision"] == 2
            assert put.json()["document"]["layout"]["positions"]["start"] == {"x": 120.0, "y": 80.0}

            first = await _publish(client, lineage, 2)
            assert first["version"] == 1
            assert first["lineage_id"] == lineage
            assert first["is_current"] is True
            assert first["content_hash"].startswith("sha256:")
            assert first["node_count"] == 1

            versions = (await client.get(f"/api/v1/workflow/lineages/{lineage}/versions")).json()
            assert versions["count"] == 1
            assert versions["items"][0]["workflow_id"] == first["workflow_id"]

            version_document = await client.get(f"/api/v1/workflow/versions/{first['workflow_id']}")
            assert version_document.status_code == 200, version_document.text
            stored = version_document.json()
            assert stored["description"] == "第一版说明"
            assert stored["layout"]["positions"]["start"] == {"x": 120.0, "y": 80.0}
            assert stored["lineage_id"] == lineage

    async def test_重命名同步整条版本族(self, container: PlatformContainer) -> None:
        async with _client(container) as client:
            draft = await _new_draft(client, "改名前")
            lineage = draft["lineage_id"]
            await _publish(client, lineage, draft["revision"])

            renamed = await client.patch(f"/api/v1/workflow/lineages/{lineage}", json={"name": "改名后"})
            assert renamed.status_code == 200, renamed.text
            body = renamed.json()
            assert body["name"] == "改名后"
            assert body["draft"]["document"]["name"] == "改名后"

            versions = (await client.get(f"/api/v1/workflow/lineages/{lineage}/versions")).json()
            assert {item["name"] for item in versions["items"]} == {"改名后"}

            old = await client.patch(f"/api/v1/workflow/lineages/{lineage}", json={"name": "改名前"})
            assert old.status_code == 200
            assert old.json()["name"] == "改名前"


class TestConflicts:
    async def test_revision_冲突带服务器版本号(self, container: PlatformContainer) -> None:
        async with _client(container) as client:
            draft = await _new_draft(client, "revision 冲突")
            lineage = draft["lineage_id"]
            document = draft["document"]
            document["description"] = "第一次修改"
            await client.put(f"/api/v1/workflow/lineages/{lineage}/draft", json={"base_revision": 1, "document": document})

            stale = await client.put(
                f"/api/v1/workflow/lineages/{lineage}/draft",
                json={"base_revision": 1, "document": draft["document"]},
            )
            assert stale.status_code == 409
            detail = _error(stale)
            assert detail["code"] == "revision_conflict"
            assert detail["details"]["server_revision"] == 2

    async def test_内容未变时_publish_拒绝空版本(self, container: PlatformContainer) -> None:
        async with _client(container) as client:
            draft = await _new_draft(client, "无变化发布")
            lineage = draft["lineage_id"]
            await _publish(client, lineage, draft["revision"])

            current = (await client.get(f"/api/v1/workflow/lineages/{lineage}/draft")).json()
            again = await client.post(
                f"/api/v1/workflow/lineages/{lineage}/publish",
                json={"base_revision": current["revision"]},
            )
            assert again.status_code == 409
            assert _error(again)["code"] == "no_changes"
            versions = (await client.get(f"/api/v1/workflow/lineages/{lineage}/versions")).json()
            assert versions["count"] == 1

    async def test_重名_409_且不改变原名称(self, container: PlatformContainer) -> None:
        async with _client(container) as client:
            first = await _new_draft(client, "甲流程")
            second = await _new_draft(client, "乙流程")
            await _publish(client, first["lineage_id"], first["revision"])
            await _publish(client, second["lineage_id"], second["revision"])

            conflict = await client.patch(
                f"/api/v1/workflow/lineages/{second['lineage_id']}",
                json={"name": "甲流程"},
            )
            assert conflict.status_code == 409
            assert _error(conflict)["code"] == "name_conflict"
            versions = (await client.get(f"/api/v1/workflow/lineages/{second['lineage_id']}/versions")).json()
            assert {item["name"] for item in versions["items"]} == {"乙流程"}

    async def test_归档后发布_409_archived(self, container: PlatformContainer) -> None:
        async with _client(container) as client:
            draft = await _new_draft(client, "归档后发布")
            lineage = draft["lineage_id"]
            published = await _publish(client, lineage, draft["revision"])

            archived = await client.post(f"/api/v1/workflow/definitions/{published['workflow_id']}/archive")
            assert archived.status_code == 200

            current = (await client.get(f"/api/v1/workflow/lineages/{lineage}/draft")).json()
            current["document"]["description"] = "归档后的修改"
            edited = await client.put(
                f"/api/v1/workflow/lineages/{lineage}/draft",
                json={"base_revision": current["revision"], "document": current["document"]},
            )
            assert edited.status_code == 200

            blocked = await client.post(
                f"/api/v1/workflow/lineages/{lineage}/publish",
                json={"base_revision": edited.json()["revision"]},
            )
            assert blocked.status_code == 409
            assert _error(blocked)["code"] == "archived"


class TestRevert:
    async def test_恢复旧版本为草稿不删除后续版本(self, container: PlatformContainer) -> None:
        async with _client(container) as client:
            draft = await _new_draft(client, "版本恢复")
            lineage = draft["lineage_id"]
            document = draft["document"]
            document["description"] = "第一版"
            await client.put(f"/api/v1/workflow/lineages/{lineage}/draft", json={"base_revision": 1, "document": document})
            first = await _publish(client, lineage, 2)

            current = (await client.get(f"/api/v1/workflow/lineages/{lineage}/draft")).json()
            current["document"]["description"] = "第二版"
            edited = await client.put(
                f"/api/v1/workflow/lineages/{lineage}/draft",
                json={"base_revision": current["revision"], "document": current["document"]},
            )
            await _publish(client, lineage, edited.json()["revision"])
            versions = (await client.get(f"/api/v1/workflow/lineages/{lineage}/versions")).json()
            assert [item["version"] for item in versions["items"]] == [2, 1]
            assert [item["is_current"] for item in versions["items"]] == [True, False]

            reverted = await client.post(
                f"/api/v1/workflow/lineages/{lineage}/revert-to-draft",
                json={"workflow_id": first["workflow_id"]},
            )
            assert reverted.status_code == 200, reverted.text
            body = reverted.json()
            assert body["document"]["description"] == "第一版"
            assert body["base_workflow_id"] == first["workflow_id"]

            after = (await client.get(f"/api/v1/workflow/lineages/{lineage}/versions")).json()
            assert after["count"] == 2, "恢复为草稿不删除历史版本"

            third = await _publish(client, lineage, body["revision"])
            assert third["version"] == 3


class TestValidationAndNotFound:
    async def test_不支持的_schema_version_422(self, container: PlatformContainer) -> None:
        async with _client(container) as client:
            draft = await _new_draft(client, "schema 版本")
            lineage = draft["lineage_id"]
            document = dict(draft["document"])
            document["schema_version"] = 2
            response = await client.put(
                f"/api/v1/workflow/lineages/{lineage}/draft",
                json={"base_revision": 1, "document": document},
            )
            assert response.status_code == 422
            assert _error(response)["code"] == "invalid_schema_version"

    async def test_路径与文档_lineage_不一致_422(self, container: PlatformContainer) -> None:
        async with _client(container) as client:
            draft = await _new_draft(client, "lineage 错位")
            document = dict(draft["document"])
            document["lineage_id"] = "wf_" + "0" * 12
            response = await client.put(
                f"/api/v1/workflow/lineages/{draft['lineage_id']}/draft",
                json={"base_revision": 1, "document": document},
            )
            assert response.status_code == 422
            assert _error(response)["code"] == "validation_failed"

    async def test_未知_lineage_的草稿与版本列表_404(self, container: PlatformContainer) -> None:
        missing = "wf_" + "f" * 12
        async with _client(container) as client:
            draft = await client.get(f"/api/v1/workflow/lineages/{missing}/draft")
            assert draft.status_code == 404
            assert _error(draft)["code"] == "not_found"

            versions = await client.get(f"/api/v1/workflow/lineages/{missing}/versions")
            assert versions.status_code == 404
            assert _error(versions)["code"] == "not_found"

            document = await client.get(f"/api/v1/workflow/versions/{missing}")
            assert document.status_code == 404
            assert _error(document)["code"] == "not_found"


class TestCors:
    async def test_草稿的_PUT_与重命名的_PATCH_预检被放行(self, container: PlatformContainer) -> None:
        """浏览器预检只认服务端声明的方法：漏了 PUT/PATCH，端点再对页面也调不到。"""
        async with _client(container) as client:
            response = await client.options(
                "/api/v1/workflow/lineages/wf_ffffffffffff/draft",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "PUT",
                },
            )
            assert response.status_code == 200
            allowed = response.headers.get("access-control-allow-methods", "")
            assert "PUT" in allowed and "PATCH" in allowed


class TestLegacyDefinitions:
    async def test_旧创建与修订仍可用且共用一条_lineage(self, container: PlatformContainer) -> None:
        async with _client(container) as client:
            created = await client.post(
                "/api/v1/workflow/definitions",
                json={
                    "name": "旧口径流程",
                    "nodes": [{"node_id": "start", "type": "start", "config": {"variables": []}}],
                    "edges": [],
                },
            )
            assert created.status_code == 201, created.text
            first = created.json()

            revised = await client.post(
                f"/api/v1/workflow/definitions/{first['workflow_id']}/revise",
                json={"description": "修订一版"},
            )
            assert revised.status_code == 200, revised.text
            second = revised.json()
            assert second["version"] == 2

            rows = (await client.get("/api/v1/workflow/definitions")).json()["items"]
            mine = [row for row in rows if row["name"] == "旧口径流程"]
            assert {row["version"] for row in mine} == {1, 2}
            assert len({row["lineage_id"] for row in mine}) == 1
            assert all(row["content_hash"].startswith("sha256:") for row in mine)

            versions = await client.get(f"/api/v1/workflow/lineages/{mine[0]['lineage_id']}/versions")
            assert versions.status_code == 200
            assert [item["version"] for item in versions.json()["items"]] == [2, 1]

            # 旧定义没有存过版式：新端点仍须能读，补一份空版式而不是 404。
            legacy_document = await client.get(f"/api/v1/workflow/versions/{first['workflow_id']}")
            assert legacy_document.status_code == 200, legacy_document.text
            assert legacy_document.json()["layout"]["positions"] == {}
