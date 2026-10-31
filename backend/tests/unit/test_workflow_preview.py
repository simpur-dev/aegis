"""试运行与实例图：dry-run 边界、独立注册表、插入节点位置与时间戳。

这里钉的是三件"看起来像发生了、其实没发生"的事：
1. 试运行跑完整张图，但 notify / api_call / device_control / warning_publish / chain_stage
   一次都不真的调用——预览的价值不能以发出一条真通知为代价；
2. 预览不占正式实例的账本（`_instances` 不增），结果只从 `/previews/{id}` 取；
3. 实例详情能重建整张图（含运行中插入的节点与位置），时间戳来自真实终态。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from aegis.config import Settings
from aegis.workflow.documents import Position, WorkflowDocument, WorkflowLayout
from aegis.workflow.engine import WorkflowEngine
from aegis.workflow.errors import WorkflowAppError, WorkflowErrorCode
from aegis.workflow.model import EdgeDef, NodeDef
from aegis.workflow.nodes import WorkflowServices
from aegis.workflow.preview import (
    MAX_PREVIEW_PAYLOAD_BYTES,
    InMemoryPreviewStore,
    PreviewLimitReached,
    dry_run_services,
)
from aegis.workflow.store import WorkflowRepository, build_definition

SEED = "wf_" + "2" * 12
LINEAGE = SEED


def _node(node_id: str, type_name: str, config: dict[str, Any] | None = None) -> NodeDef:
    return NodeDef(node_id=node_id, type=type_name, name=node_id, config=config or {})


def _document(
    nodes: list[NodeDef],
    edges: list[tuple[str, str]],
    *,
    positions: dict[str, Position] | None = None,
) -> WorkflowDocument:
    return WorkflowDocument(
        lineage_id=LINEAGE,
        name="试运行图",
        nodes=nodes,
        edges=[EdgeDef(source=source, target=target) for source, target in edges],
        layout=WorkflowLayout(positions=positions or {}),
    )


def _settings() -> Settings:
    return Settings(env="test", bus_backend="memory", store_backend="memory", llm_api_key="")


def _side_effect_graph_document() -> WorkflowDocument:
    return _document(
        [
            _node("start", "start", {"variables": []}),
            _node("identify", "hazard_identify"),
            _node("call", "api_call", {"method": "POST", "url": "https://example.invalid/hook"}),
            _node("device", "device_control", {"device": "siren", "action": "on", "command_url": "https://example.invalid/device"}),
            _node("publish", "warning_publish", {"channels": ["sms", "app"]}),
            _node("notify", "notify", {"level": "warning", "text": "撤离"}),
            _node("stage", "chain_stage", {"stage": "perceive"}),
            _node("review", "human_review", {"options": ["approve", "reject"]}),
            _node("end", "end"),
        ],
        [
            ("start", "identify"),
            ("identify", "call"),
            ("call", "device"),
            ("device", "publish"),
            ("publish", "notify"),
            ("notify", "stage"),
            ("stage", "review"),
            ("review", "end"),
        ],
        positions={
            "start": Position(x=0, y=0),
            "identify": Position(x=280, y=0),
            "call": Position(x=560, y=0),
            "device": Position(x=840, y=0),
            "publish": Position(x=1120, y=0),
            "notify": Position(x=1400, y=0),
            "stage": Position(x=1680, y=0),
            "review": Position(x=1960, y=0),
            "end": Position(x=2240, y=0),
        },
    )


class TestPreview:
    async def test_试运行零外部调用且不进实例账本(self) -> None:
        calls: list[str] = []

        async def spy_identify(payload: dict[str, Any]) -> dict[str, Any]:
            calls.append("identify")
            return {"hazards": ["landslide"], "hits": [], "seen": payload.get("region")}

        async def spy_notify(_: dict[str, Any]) -> None:
            calls.append("notify")

        async def spy_http(*_: Any) -> dict[str, Any]:
            calls.append("http_call")
            return {"ok": True}

        async def spy_publish(_: dict[str, Any]) -> dict[str, Any]:
            calls.append("warning_publish")
            return {"delivered": 2}

        async def spy_chain(_: str, __: dict[str, Any]) -> dict[str, Any]:
            calls.append("chain_stage")
            return {"branch": "delivered"}

        services = WorkflowServices(
            identify=spy_identify,
            notify=spy_notify,
            http_call=spy_http,
            publish_warning=spy_publish,
            run_chain_stage=spy_chain,
        )
        engine = WorkflowEngine(services=services, settings=_settings())

        detail = await engine.preview(
            document=_side_effect_graph_document(),
            payload={"region": "R-1", "warning": {"level": 3, "text": "撤离"}},
        )

        # LLM/识别腿照常跑：试运行要看的正是它们给出的真实产出。
        assert calls == ["identify"]
        assert engine.instance_stats["held"] == 0
        assert not engine._instances
        assert detail["preview"] is True
        assert detail["preview_id"].startswith("pvp_")
        assert detail["status"] == "succeeded"
        assert engine.preview_detail(detail["preview_id"]) is not None
        assert engine.instance(detail["instance_id"]) is None

        runs = {row["node_id"]: row for row in detail["nodes"]}
        assert runs["identify"]["state"] == "succeeded"
        assert runs["call"]["output"]["response"]["dry_run"] is True
        assert runs["call"]["output"]["response"]["would_call"] == "api_call"
        assert runs["device"]["output"]["response"]["would_call"] == "api_call"
        assert runs["publish"]["output"]["delivered"] == 0
        assert runs["publish"]["output"]["would_deliver_to"] == 2
        assert runs["publish"]["output"]["branch"] == "delivered"
        assert runs["notify"]["output"]["notified"] is False
        assert runs["notify"]["output"]["would_call"] == "notify"
        assert "未发送通知" in " ".join(runs["notify"]["notes"])
        assert runs["stage"]["output"]["dry_run"] is True
        assert runs["review"]["output"]["simulated"] is True
        assert runs["review"]["output"]["decision"]["by"] == "preview"
        assert "未挂起等待人工" in " ".join(runs["review"]["notes"])
        assert detail["graph"]["positions"]["end"] == {"x": 2240.0, "y": 0.0}

    async def test_试运行结果表_上限先占位_TTL_过期可再用(self) -> None:
        now = [1000.0]
        store = InMemoryPreviewStore(limit=2, ttl_seconds=60.0, clock=lambda: now[0])
        engine = WorkflowEngine(services=WorkflowServices(), settings=_settings(), preview_store=store)
        document = _document([_node("start", "start"), _node("end", "end")], [("start", "end")])

        first = await engine.preview(document=document)
        second = await engine.preview(document=document)
        assert store.get(first["preview_id"]) is not None
        assert store.get(second["preview_id"]) is not None

        with pytest.raises(WorkflowAppError) as rejected:
            await engine.preview(document=document)
        assert rejected.value.code is WorkflowErrorCode.PREVIEW_LIMIT
        assert rejected.value.status == 409
        assert rejected.value.details["limit"] == 2
        assert len(store) == 2

        now[0] += 61.0
        third = await engine.preview(document=document)
        assert store.get(third["preview_id"]) is not None
        assert store.get(first["preview_id"]) is None
        assert len(store) == 1

    async def test_试运行入参超限在开跑前拒绝(self) -> None:
        engine = WorkflowEngine(services=WorkflowServices(), settings=_settings())
        document = _document([_node("start", "start"), _node("end", "end")], [("start", "end")])

        with pytest.raises(WorkflowAppError) as rejected:
            await engine.preview(document=document, payload={"blob": "x" * (MAX_PREVIEW_PAYLOAD_BYTES + 1)})
        assert rejected.value.code is WorkflowErrorCode.DOCUMENT_TOO_LARGE
        assert rejected.value.status == 422
        assert rejected.value.details["kind"] == "preview_payload"
        assert len(engine.preview_store) == 0

    async def test_试运行校验不过的图被拒且不占名额(self) -> None:
        engine = WorkflowEngine(services=WorkflowServices(), settings=_settings())
        document = _document(
            [_node("start", "start"), _node("j", "join", {"upstream": ["ghost"]}), _node("end", "end")],
            [("start", "j"), ("j", "end")],
        )

        with pytest.raises(WorkflowAppError) as rejected:
            await engine.preview(document=document)
        assert rejected.value.code is WorkflowErrorCode.VALIDATION_FAILED
        assert "ghost" in rejected.value.message
        assert len(engine.preview_store) == 0

    def test_注册表不变量_上限与TTL必须为正(self) -> None:
        with pytest.raises(ValueError):
            InMemoryPreviewStore(limit=0)
        with pytest.raises(ValueError):
            InMemoryPreviewStore(ttl_seconds=0.0)

    def test_并发占位后_put仍会兜底拒绝(self) -> None:
        store = InMemoryPreviewStore(limit=1)
        store.ensure_room()
        store.put("pvp_first", {"preview_id": "pvp_first"})
        with pytest.raises(PreviewLimitReached):
            store.put("pvp_second", {"preview_id": "pvp_second"})

    async def test_干跑包装的通知腿返回空回执(self) -> None:
        services = dry_run_services(WorkflowServices())
        assert services.notify is not None
        assert await services.notify({"level": "warning"}) is None


class TestInstanceGraph:
    async def _seeded_engine(self, *, with_layout: bool) -> tuple[WorkflowEngine, str]:
        repository = WorkflowRepository()
        document = _document(
            [_node("start", "start"), _node("end", "end")],
            [("start", "end")],
            positions={"start": Position(x=10, y=20), "end": Position(x=500, y=20)} if with_layout else {},
        )
        definition = build_definition(
            name="实例图",
            workflow_id=SEED,
            nodes=document.nodes,
            edges=document.edges,
        )
        stored = await repository.save_next_named(definition, document=document)
        engine = WorkflowEngine(repository=repository, services=WorkflowServices(), settings=_settings())
        return engine, stored.workflow_id

    async def test_实例详情带图结果与时间戳(self) -> None:
        engine, workflow_id = await self._seeded_engine(with_layout=True)
        detail = await engine.start(workflow_id, trace_id="trace-graph")

        assert detail["lineage_id"] == SEED
        assert detail["preview"] is False
        assert detail["preview_id"] is None
        assert detail["graph"]["positions"] == {
            "start": {"x": 10.0, "y": 20.0},
            "end": {"x": 500.0, "y": 20.0},
        }
        assert {node["node_id"] for node in detail["graph"]["nodes"]} == {"start", "end"}
        assert detail["graph"]["edges"] == [{"source": "start", "target": "end", "condition": ""}]
        assert detail["results"]["start"]["started"] is True
        assert detail["results"]["end"]["ended"] is True

        created = datetime.fromisoformat(detail["created_at"].replace("Z", "+00:00"))
        finished = datetime.fromisoformat(detail["finished_at"].replace("Z", "+00:00"))
        assert finished >= created
        assert detail["status"] == "succeeded"

    async def test_旧定义无layout时实例图仍可画(self) -> None:
        engine, workflow_id = await self._seeded_engine(with_layout=False)
        detail = await engine.start(workflow_id, trace_id="trace-no-layout")
        assert detail["graph"]["positions"] == {}
        assert len(detail["graph"]["nodes"]) == 2

    async def test_插入节点补位置且图里立即出现(self) -> None:
        engine, workflow_id = await self._seeded_engine(with_layout=True)
        detail = await engine.start(workflow_id, trace_id="trace-insert")
        instance_id = detail["instance_id"]

        await engine.insert_node(
            instance_id,
            after="start",
            node={"node_id": "notify_1", "type": "notify", "config": {"text": "插播"}},
        )
        await engine.insert_node(
            instance_id,
            after="notify_1",
            node={"node_id": "notify_2", "type": "notify", "config": {"text": "再插播"}},
            position={"x": 1, "y": 2},
        )

        after = engine.instance_detail(instance_id)
        assert after is not None
        positions = after["graph"]["positions"]
        assert positions["notify_1"] == {"x": 310.0, "y": 20.0}
        assert positions["notify_2"] == {"x": 1.0, "y": 2.0}
        assert {node["node_id"] for node in after["graph"]["nodes"]} == {"start", "end", "notify_1", "notify_2"}
        edges = {(edge["source"], edge["target"]) for edge in after["graph"]["edges"]}
        assert ("start", "notify_1") in edges
        assert ("notify_1", "notify_2") in edges
        assert ("notify_2", "end") in edges
        assert after["nodes"][2]["node_id"] == "notify_1"

    async def test_等待中是未终态_中止后写终态时间(self) -> None:
        repository = WorkflowRepository()
        document = _document(
            [
                _node("start", "start"),
                _node("review", "human_review", {"options": ["approve", "reject"]}),
                _node("end", "end"),
            ],
            [("start", "review"), ("review", "end")],
        )
        definition = build_definition(name="等待图", workflow_id=SEED, nodes=document.nodes, edges=document.edges)
        stored = await repository.save_next_named(definition, document=document)
        engine = WorkflowEngine(repository=repository, services=WorkflowServices(), settings=_settings())
        detail = await engine.start(stored.workflow_id, trace_id="trace-wait")

        assert detail["status"] == "waiting"
        assert detail["finished_at"] is None
        assert detail["nodes"][1]["state"] == "awaiting_human"

        aborted = await engine.abort(detail["instance_id"], reason="演练结束")
        assert aborted["status"] == "aborted"
        assert aborted["finished_at"] is not None
