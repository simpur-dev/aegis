"""工作流端点的 HTTP 层验证。

这个文件此前不存在——工作流那十来条路由只有压测脚本引用过，没有任何 API 级用例。
后果是"画布能存不能开"这类缺口一路活到真机巡检：`GET /definitions` 只报计数，
而根本没有"取一份完整定义"的出口，前端想打开也无从下手。

这里要钉住的是三件事：
1. 列表只给计数（够挑一条），详情给全量（够改一条）——两者不能互相顶替；
2. **取出来 → 原样存回去**必须逐字段等价：画布的"保存"走的就是这条路径，
   任何字段在映射中丢失，症状都是"我明明没改，存完却不一样了"；
3. 不存在的 workflow_id 要 404，而不是返回一个空定义让前端画出空白画布。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from aegis.api.app import create_app
from aegis.config import Settings
from aegis.container import PlatformContainer, create_container


def _settings(**overrides: Any) -> Settings:
    return Settings(
        env="test",
        bus_backend="memory",
        store_backend="memory",
        simulator_enabled=False,
        delivery_mode="mock",
        llm_api_key="",
        **overrides,
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
    yield ctn
    await ctn.shutdown()


@pytest.mark.asyncio
async def test_列表只报计数不报内容(container: PlatformContainer) -> None:
    async with _client(container) as client:
        body = (await client.get("/api/v1/workflow/definitions")).json()
        assert body["count"] >= 5, f"内置模板应有 5 灾种 + 核签流程，实得 {body['count']}"
        row = body["items"][0]
        assert {"workflow_id", "name", "version", "node_count", "edge_count", "status"} <= set(row)
        assert "nodes" not in row, "列表里塞全量节点会让每次刷新都拖着一整张图"


@pytest.mark.asyncio
async def test_详情给出可编辑的完整定义(container: PlatformContainer) -> None:
    async with _client(container) as client:
        listed = (await client.get("/api/v1/workflow/definitions")).json()["items"]
        target = next(row for row in listed if row["node_count"] > 1)
        detail = await client.get(f"/api/v1/workflow/definitions/{target['workflow_id']}")
        assert detail.status_code == 200, detail.text
        definition = detail.json()
        assert definition["workflow_id"] == target["workflow_id"]
        assert len(definition["nodes"]) == target["node_count"], "计数与内容对不上，画布会少画节点"
        assert len(definition["edges"]) == target["edge_count"]
        node = definition["nodes"][0]
        assert {"node_id", "type", "config", "sla_ms", "timeout_ms", "on_failure", "retry"} <= set(node)


@pytest.mark.asyncio
async def test_取出来原样存回去不丢字段(container: PlatformContainer) -> None:
    """画布"打开→保存"这条路径的等价性保证。

    前端把定义映射成图、再映射回定义；任何一映射漏字段，症状都是"我没改什么，
    存完版本却变了内容"。这里用后端自己的读写口做往返，把字段逐个比一遍。
    """
    async with _client(container) as client:
        listed = (await client.get("/api/v1/workflow/definitions")).json()["items"]
        target = next(row for row in listed if row["node_count"] > 1)
        original = (await client.get(f"/api/v1/workflow/definitions/{target['workflow_id']}")).json()

        created = await client.post(
            "/api/v1/workflow/definitions",
            json={
                "name": f"往返副本-{original['workflow_id'][-6:]}",
                "description": original["description"],
                "nodes": original["nodes"],
                "edges": original["edges"],
            },
        )
        assert created.status_code == 201, created.text  # 创建返回 201，不是 200
        new_id = created.json()["workflow_id"]
        round_tripped = (await client.get(f"/api/v1/workflow/definitions/{new_id}")).json()

        assert len(round_tripped["nodes"]) == len(original["nodes"])
        for before, after in zip(original["nodes"], round_tripped["nodes"], strict=True):
            assert after == before, f"节点 {before['node_id']} 往返后变了：{after}"
        assert round_tripped["edges"] == original["edges"], "连线（含条件）往返后变了"


@pytest.mark.asyncio
async def test_名称与说明里的首尾空格不进定义(container: PlatformContainer) -> None:
    """人在画布上多敲一个空格，不该变成"两个看着同名的节点"。

    三个写模型（`NodeInput`/`DefinitionInput`/`ReviseInput`）共用一个混入裁剪器，
    而它用的是 `check_fields=False`——字段改名后校验器会**悄悄不生效**，光读代码是绿的。
    所以三条出口各钉一次真行为：创建回执、存进去的定义、修订后的定义。
    """
    async with _client(container) as client:
        listed = (await client.get("/api/v1/workflow/definitions")).json()["items"]
        target = next(row for row in listed if row["node_count"] > 1)
        nodes = (await client.get(f"/api/v1/workflow/definitions/{target['workflow_id']}")).json()["nodes"]
        edges = (await client.get(f"/api/v1/workflow/definitions/{target['workflow_id']}")).json()["edges"]
        padded_nodes = [{**node, "name": f"  {node['name']}  "} for node in nodes]

        created = await client.post(
            "/api/v1/workflow/definitions",
            json={"name": "  夜间巡检流程  ", "description": "  一行说明  ", "nodes": padded_nodes, "edges": edges},
        )
        assert created.status_code == 201, created.text
        new_id = created.json()["workflow_id"]
        assert created.json()["name"] == "夜间巡检流程", "创建回执里的名字还带着空格"

        stored = (await client.get(f"/api/v1/workflow/definitions/{new_id}")).json()
        assert stored["description"] == "一行说明"
        assert [node["name"] for node in stored["nodes"]] == [node["name"] for node in nodes], "节点名空格里外都没裁"

        revised = await client.post(f"/api/v1/workflow/definitions/{new_id}/revise", json={"description": "  改一版  "})
        assert revised.status_code == 200, revised.text
        # 修订是"新版本、新 workflow_id"（旧那份仍在，供在途实例绑定）：读回执里的那个号
        revised_id = revised.json()["workflow_id"]
        after = (await client.get(f"/api/v1/workflow/definitions/{revised_id}")).json()
        assert after["description"] == "改一版"
        assert after["version"] > stored["version"]


@pytest.mark.asyncio
async def test_全空白的流程名是422不是500(container: PlatformContainer) -> None:
    """`"    "` 裁完是空串，`min_length=2` 就该在这儿判死。

    不裁的话四个空格能过长度校验，一路存进定义，界面上是一行"看不见的名字"。
    """
    async with _client(container) as client:
        response = await client.post(
            "/api/v1/workflow/definitions",
            json={"name": "    ", "description": "", "nodes": [], "edges": []},
        )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert any(item["loc"][-1] == "name" for item in detail), f"422 里点名不了是哪一格：{detail}"


@pytest.mark.asyncio
async def test_不存在的定义给404而不是空白定义(container: PlatformContainer) -> None:
    async with _client(container) as client:
        response = await client.get("/api/v1/workflow/definitions/wf_000000000000")
        assert response.status_code == 404
        # 空 body 会让前端画出"一张空白画布"，看起来像定义本身是空的
        assert "nodes" not in response.json()


@pytest.mark.asyncio
async def test_节点类型出口与后端注册表同源(container: PlatformContainer) -> None:
    async with _client(container) as client:
        body = (await client.get("/api/v1/workflow/node-types")).json()
        names = {str(row["type"]) for row in body["items"]}
        assert {"hazard_identify", "risk_assess", "situation_simulate", "human_review", "degrade_to_rule"} <= names
        assert body["count"] == len(names) == 16, f"节点类型数变了（{len(names)}），前端镜像与文档都要跟着改"


async def _copy_of_builtin(client: httpx.AsyncClient, tag: str) -> str:
    """借一份内置定义的节点/连线建副本：省得在测试里重抄图，图一变就维护不住。"""
    listed = (await client.get("/api/v1/workflow/definitions")).json()["items"]
    target = next(row for row in listed if row["node_count"] > 1)
    source = (await client.get(f"/api/v1/workflow/definitions/{target['workflow_id']}")).json()
    created = await client.post(
        "/api/v1/workflow/definitions",
        json={
            "name": f"归档可逆-{tag}-{source['workflow_id'][-6:]}",
            "description": "",
            "nodes": source["nodes"],
            "edges": source["edges"],
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["workflow_id"])


@pytest.mark.asyncio
async def test_归档与取消归档是一对可逆动作(container: PlatformContainer) -> None:
    """定义列表上这两个按钮是值班员唯一能"收走一套剧本"的入口。
    此前归档一按生效、只有一行提示，且没有任何出口把它带回来——
    误归档内置核签流程之后，本班次每条低置信度上报都开不出工单。"""
    async with _client(container) as client:
        workflow_id = await _copy_of_builtin(client, "往返")

        archived = await client.post(f"/api/v1/workflow/definitions/{workflow_id}/archive")
        assert archived.status_code == 200, archived.text
        assert archived.json()["status"] == "archived"
        assert (await client.get(f"/api/v1/workflow/definitions/{workflow_id}")).json()["status"] == "archived"

        restored = await client.post(f"/api/v1/workflow/definitions/{workflow_id}/restore")
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "active"
        detail = (await client.get(f"/api/v1/workflow/definitions/{workflow_id}")).json()
        assert detail["status"] == "active"
        assert detail["version"] == 1, "撤销归档不该产生新版本：一撤就多一版，版本链就成了噪声"


@pytest.mark.asyncio
async def test_取消归档只认同名最新版并说清该撤哪一版(container: PlatformContainer) -> None:
    """按名字取用剧本的路径（核签开单、按名启动）只看最新版。
    把旧版签回启用中会显示"已恢复"而工单照样开不出——静默无效比报错危险，所以当场拒绝并指名。"""
    async with _client(container) as client:
        first = await _copy_of_builtin(client, "旧版")
        revised = await client.post(f"/api/v1/workflow/definitions/{first}/revise", json={"description": "改一版"})
        assert revised.status_code == 200, revised.text
        second = str(revised.json()["workflow_id"])
        await client.post(f"/api/v1/workflow/definitions/{second}/archive")

        refused = await client.post(f"/api/v1/workflow/definitions/{first}/restore")
        assert refused.status_code == 400, refused.text
        assert "v2" in refused.json()["detail"], f"报错要指名该恢复哪一版：{refused.json()}"

        restored = await client.post(f"/api/v1/workflow/definitions/{second}/restore")
        assert restored.status_code == 200, restored.text
        assert restored.json()["status"] == "active"


@pytest.mark.asyncio
async def test_取消归档遇到不存在的定义给404(container: PlatformContainer) -> None:
    async with _client(container) as client:
        response = await client.post("/api/v1/workflow/definitions/wf_000000000000/restore")
        assert response.status_code == 404, response.text
        # 404 而不是 400：前端按"这条不在了、去刷新列表"处理，不该把它说成参数错
        assert "不存在" in response.json()["detail"]
