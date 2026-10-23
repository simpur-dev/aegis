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

import asyncio
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


def _one_node_payload(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": "",
        "nodes": [{"node_id": "a", "type": "delay", "name": "等一会儿", "config": {"seconds": 0}}],
        "edges": [],
    }


@pytest.mark.asyncio
async def test_旧端点同名再建一份归入同一族谱(container: PlatformContainer) -> None:
    """旧 create 的语义是"同名再建 = 追加新版本"（模板 force 与引擎用例都依赖），
    锁内分配只保证版本号不撞，不把同名改成冲突。"""
    async with _client(container) as client:
        payload = _one_node_payload("旧端点重名流程")
        first = await client.post("/api/v1/workflow/definitions", json=payload)
        assert first.status_code == 201, first.text
        second = await client.post("/api/v1/workflow/definitions", json=payload)
        assert second.status_code == 201, second.text
        assert (first.json()["version"], second.json()["version"]) == (1, 2)

        first_detail = (await client.get(f"/api/v1/workflow/definitions/{first.json()['workflow_id']}")).json()
        second_detail = (await client.get(f"/api/v1/workflow/definitions/{second.json()['workflow_id']}")).json()
        assert first_detail["lineage_id"] == second_detail["lineage_id"] == first.json()["workflow_id"]
        versions = await client.get(f"/api/v1/workflow/lineages/{first.json()['workflow_id']}/versions")
        assert [row["version"] for row in versions.json()["items"]] == [2, 1], "版本列表最新在前"


@pytest.mark.asyncio
async def test_旧修订并发时不撞版本号(container: PlatformContainer) -> None:
    """锁外读 next_version 时两路修订会同时抢 v2，一路冒成 500；锁内分配应各得一号。"""
    async with _client(container) as client:
        created = await client.post("/api/v1/workflow/definitions", json=_one_node_payload("旧端点并发修订流程"))
        assert created.status_code == 201, created.text
        workflow_id = created.json()["workflow_id"]
        first, second = await asyncio.gather(
            client.post(f"/api/v1/workflow/definitions/{workflow_id}/revise", json={"description": "并一"}),
            client.post(f"/api/v1/workflow/definitions/{workflow_id}/revise", json={"description": "并二"}),
        )
        assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
        assert sorted([first.json()["version"], second.json()["version"]]) == [2, 3]

        # 新族的 lineage 就是最早那一版的 workflow_id：版本列表必须把三版都聚在同一条族谱里。
        versions = await client.get(f"/api/v1/workflow/lineages/{workflow_id}/versions")
        assert versions.status_code == 200, versions.text
        assert [row["version"] for row in versions.json()["items"]] == [3, 2, 1]


@pytest.mark.asyncio
async def test_旧创建违反文档不变量时是400不是500(container: PlatformContainer) -> None:
    """注册 start/end 后，旧端点也能送来"两个起点"这类违反文档不变量的图。

    文档校验抛的是 Pydantic ValidationError；不在这里收敛就会冒成 500，
    让调用方以为服务端坏了，实际该改的是图。
    """
    async with _client(container) as client:
        response = await client.post(
            "/api/v1/workflow/definitions",
            json={
                "name": "两个起点",
                "description": "",
                "nodes": [
                    {"node_id": "start_a", "type": "start", "name": "起点一", "config": {}},
                    {"node_id": "start_b", "type": "start", "name": "起点二", "config": {}},
                ],
                "edges": [],
            },
        )
        assert response.status_code == 400, response.text
        assert "start" in response.json()["detail"]


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
        assert body["count"] == len(names) == 18, f"节点类型数变了（{len(names)}），前端镜像与文档都要跟着改"


@pytest.mark.asyncio
async def test_内部段原语给引擎看不给画布看(container: PlatformContainer) -> None:
    """`chain_stage` 要的是链路盘面（ChainBoard），从画布拖出来必然失败，所以它只进引擎注册表。

    这条盯的是两个口径不许互相顶掉：面板少了它（值班员少一个死选项），引擎不能少它
    （标准五段模板就建在它上面，分派矩阵用例负责证明"引擎真的驱动过"）。
    """
    async with _client(container) as client:
        names = {str(row["type"]) for row in (await client.get("/api/v1/workflow/node-types")).json()["items"]}
    registered = set(container.workflow.node_registry.names())
    assert "chain_stage" in registered, "引擎注册表里没了它，标准五段模板就是空图"
    assert "chain_stage" not in names
    assert registered - names == {"chain_stage"}, "内部原语只有这一类：再多出来一个就说明有人把面板口径当注册表口径写了"


@pytest.mark.asyncio
async def test_核签台账的签字人来自提交者而不是接口默认(container: PlatformContainer) -> None:
    """平台没有登录态：`by` 只能是签字的人自己写的字。

    前端此前把它固定写成"值班指挥员"，于是台账上那句"谁签的"从来不是一条事实，而是一句套话。
    接口这边要钉住两件事：填了就用填的，没填就落到 `unknown`（那是"没人署名"的样子，
    不是某个角色的名字）。
    """
    async with _client(container) as client:
        started = await client.post("/api/v1/workflow/instances", json={"workflow_name": "人工上报核签流程", "payload": {}})
        assert started.status_code == 200, started.text
        instance_id = started.json()["instance_id"]

        anonymous = await client.post(
            f"/api/v1/workflow/instances/{instance_id}/nodes/review/decision",
            json={"choice": "approve", "comment": ""},
        )
        assert anonymous.status_code == 200, anonymous.text
        review = next(row for row in anonymous.json()["nodes"] if row["node_id"] == "review")
        assert review["output"]["decision"]["by"] == "unknown", "没署名就不能凭空有个名字"

        second = await client.post("/api/v1/workflow/instances", json={"workflow_name": "人工上报核签流程", "payload": {}})
        second_id = second.json()["instance_id"]
        named = await client.post(
            f"/api/v1/workflow/instances/{second_id}/nodes/review/decision",
            json={"choice": "adjust", "by": "巡护员扎西", "comment": "改判滑坡"},
        )
        assert named.status_code == 200, named.text
        signed = next(row for row in named.json()["nodes"] if row["node_id"] == "review")
        assert signed["output"]["decision"]["by"] == "巡护员扎西", "写进去的名字要原样留在台账里"
        assert signed["output"]["decision"]["comment"] == "改判滑坡"


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


DECLARED_ONLY = "发生泥石流，请求红色预警"


async def _open_review_ticket(client: httpx.AsyncClient, region_code: str) -> dict[str, Any]:
    """走真接入口开一张核签工单：标注池的标记只有 `ReportIntake.submit` 这条路会打，
    测试自己拼 payload 就永远测不到"工单其实没带标记"这一种失败。"""
    opened = await client.post("/api/v1/reports", json={"note": DECLARED_ONLY, "region_code": region_code, "reporter": "村民"})
    assert opened.status_code == 200, opened.text
    review = opened.json()["review"]
    assert review is not None and review["pending_node"] == "review", f"低置信度上报要真开出工单：{opened.json()}"
    return review


@pytest.mark.asyncio
async def test_签完工单要当面对签字人说清这一票落没落进真值表(container: PlatformContainer) -> None:
    """数据飞轮（D9）的接口出口：回给签字人的不是"签好了"三个字，而是这一票的去向。

    默认装配是 `store_backend=memory`，没有标注面可写，所以这里成立的事实是"如实说未沉淀 +
    附上样本"。伪造一个"已沉淀"、或干脆不带这个键，症状都是现场以为飞轮在转而真值表始终为零。
    样本能不能真写进 `warning_truth_labels`，由 unit 用例对着持久层那个真裁判函数钉。
    """
    async with _client(container) as client:
        review = await _open_review_ticket(client, "540200")
        instance = (await client.get(f"/api/v1/workflow/instances/{review['instance_id']}")).json()["payload"]

        signed = await client.post(
            f"/api/v1/workflow/instances/{review['instance_id']}/nodes/review/decision",
            json={"choice": "approve", "by": "值班员普赤"},
        )
        assert signed.status_code == 200, signed.text
        captured = signed.json()["标注沉淀"]
        assert captured["status"] == "未沉淀" and "memory" in captured["原因"], captured
        sample = captured["样本"]
        assert sample["case_id"] == instance["event_id"], "幂等键必须是这个事件，而不是每次签单新生成一个"
        assert sample["truth_level"] == instance["risk_level"], "批准就是把工单上那个等级确认为真值，不另算一套"
        assert sample["labelled_by"] == "值班员普赤"
        assert container.labels is not None and container.labels.stats["未沉淀"] == 1


@pytest.mark.asyncio
async def test_改判带等级才成真值缺等级只记账不替人编(container: PlatformContainer) -> None:
    async with _client(container) as client:
        blank = await _open_review_ticket(client, "540300")
        refused = await client.post(
            f"/api/v1/workflow/instances/{blank['instance_id']}/nodes/review/decision",
            json={"choice": "adjust", "by": "值班员普赤"},
        )
        assert refused.status_code == 200, "改判缺等级不该让签字本身失败——那一票已经签了，只是成不了真值"
        captured = refused.json()["标注沉淀"]
        assert captured["status"] == "未沉淀" and "level" in captured["原因"], captured
        assert "样本" not in captured
        assert container.labels is not None and container.labels.stats == {"已沉淀": 0, "未沉淀": 0, "映射失败": 1}

        revised = await _open_review_ticket(client, "540400")
        signed = await client.post(
            f"/api/v1/workflow/instances/{revised['instance_id']}/nodes/review/decision",
            json={"choice": "adjust", "by": "值班员普赤", "level": 5},
        )
        assert signed.status_code == 200, signed.text
        assert signed.json()["标注沉淀"]["样本"]["truth_level"] == 5, "改判到红色却落原等级，等于把这一笔改判抹掉了"


@pytest.mark.asyncio
async def test_没有结论裁决标记的会签不进标注池(container: PlatformContainer) -> None:
    """演练放行、画布上手工起的那类会签，判的不是"某条结论成不成立"。
    把它们也当真值喂进真值表，准确率就成了自证——所以标记缺失时必须完全不碰标注面。"""
    async with _client(container) as client:
        started = await client.post("/api/v1/workflow/instances", json={"workflow_name": "人工上报核签流程", "payload": {}})
        instance_id = started.json()["instance_id"]
        signed = await client.post(
            f"/api/v1/workflow/instances/{instance_id}/nodes/review/decision",
            json={"choice": "approve", "by": "值班员普赤"},
        )
        assert signed.status_code == 200, signed.text
        assert "标注沉淀" not in signed.json(), signed.json()
        assert container.labels is not None and sum(container.labels.stats.values()) == 0, "没标记就不该有任何一条账"
