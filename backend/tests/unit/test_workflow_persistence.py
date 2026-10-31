"""P1-5：定义写穿、实例检查点与重启恢复。

不连库：用内存替身模拟持久层，重点验证引擎在"进程重启"两侧的语义——
等待人工的实例能接回并继续，运行中的实例必须显式收成中断而不是悄悄消失。
"""

from __future__ import annotations

import asyncio

import pytest

from aegis.config import Settings
from aegis.observability.tracer import Tracer
from aegis.workflow.engine import WorkflowEngine
from aegis.workflow.model import InstanceStatus, WorkflowDef
from aegis.workflow.nodes import WorkflowServices
from aegis.workflow.persistence import InstanceCheckpoint


class FakePersistence:
    """按 workflow_id 覆盖定义、按实例保留最新检查点：足够复现"重启后再读"的语义。"""

    def __init__(self) -> None:
        self.definitions: dict[str, WorkflowDef] = {}
        self.checkpoints: list[InstanceCheckpoint] = []

    def save_definition(self, definition: WorkflowDef) -> None:
        self.definitions[definition.workflow_id] = definition.model_copy(deep=True)

    def save_instance(self, checkpoint: InstanceCheckpoint) -> None:
        self.checkpoints.append(
            InstanceCheckpoint(
                instance=checkpoint.instance.model_copy(deep=True),
                runtime=checkpoint.runtime.model_copy(deep=True),
            )
        )

    async def load_definitions(self, *, limit: int) -> list[WorkflowDef]:
        return list(self.definitions.values())[:limit]

    async def load_open_instances(self, *, limit: int) -> list[InstanceCheckpoint]:
        latest: dict[str, InstanceCheckpoint] = {}
        for checkpoint in self.checkpoints:
            instance_id = checkpoint.instance.instance_id
            if checkpoint.instance.status in (InstanceStatus.RUNNING, InstanceStatus.WAITING):
                latest[instance_id] = checkpoint
            else:
                latest.pop(instance_id, None)
        ordered = sorted(latest.values(), key=lambda item: (item.instance.created_at, item.instance.instance_id))
        return ordered[:limit]


def make_engine(persistence: FakePersistence | None = None, *, services: WorkflowServices | None = None) -> WorkflowEngine:
    return WorkflowEngine(
        persistence=persistence,
        services=services or WorkflowServices(),
        tracer=Tracer(),
        settings=Settings(env="test"),
    )


def node(node_id: str, type_name: str, config: dict | None = None) -> dict:
    return {"node_id": node_id, "type": type_name, "config": config or {}}


class TestDefinitionWriteThrough:
    async def test_每次保存都写穿且重启恢复不追加版本(self) -> None:
        persistence = FakePersistence()
        first = make_engine(persistence)
        created = await first.create_definition(name="持久化流程", nodes=[node("a", "delay", {"seconds": 0})], edges=[])
        assert set(persistence.definitions) == {created.workflow_id}

        revised = await first.revise_definition(created.workflow_id, description="第二版")
        assert set(persistence.definitions) == {created.workflow_id, revised.workflow_id}

        second = make_engine(persistence)
        stats = await second.restore_persisted_state()
        assert stats["definitions"] == 2
        restored = second.latest_definition("持久化流程")
        assert restored is not None
        assert (restored.version, restored.description, restored.lineage_id) == (2, "第二版", created.lineage_id)
        # 恢复是内存索引重建，不得把同一批定义再写回持久层一遍。
        assert set(persistence.definitions) == {created.workflow_id, revised.workflow_id}

    async def test_恢复幂等(self) -> None:
        persistence = FakePersistence()
        first = make_engine(persistence)
        await first.create_definition(name="幂等流程", nodes=[node("a", "delay", {"seconds": 0})], edges=[])

        second = make_engine(persistence)
        assert (await second.restore_persisted_state())["definitions"] == 1
        assert (await second.restore_persisted_state())["definitions"] == 0


class TestInstanceRecovery:
    async def test_等待人工的实例重启后可续并保留现场(self) -> None:
        persistence = FakePersistence()
        first = make_engine(persistence)
        workflow = await first.create_definition(
            name="核签流程",
            nodes=[node("sign", "human_review", {"prompt": "是否发布", "options": ["approve", "reject"]})],
            edges=[],
        )
        detail = await first.start(workflow.workflow_id, trace_id="trc_" + "a" * 16, payload={"warning_id": "wrn_x"})
        assert detail["status"] == "waiting"
        instance_id = detail["instance_id"]

        second = make_engine(persistence)
        stats = await second.restore_persisted_state()
        assert stats == {"definitions": 1, "instances": 1, "interrupted": 0}

        restored = second.instance_detail(instance_id)
        assert restored is not None
        assert restored["recovered"] is True and restored["interrupted"] is False
        assert restored["status"] == "waiting"
        assert restored["payload"] == {"warning_id": "wrn_x"}
        sign = next(item for item in restored["nodes"] if item["node_id"] == "sign")
        assert sign["state"] == "awaiting_human"

        resumed = await second.resume(instance_id, node_id="sign", decision={"choice": "approve", "by": "指挥员"})
        assert resumed["status"] == "succeeded"

    async def test_运行中的实例重启后显式中断而不是悄悄消失(self) -> None:
        gate = asyncio.Event()

        async def query(**_kwargs: object) -> list[dict]:
            await gate.wait()
            return []

        persistence = FakePersistence()
        first = make_engine(persistence, services=WorkflowServices(telemetry_query=query))
        workflow = await first.create_definition(
            name="运行中流程",
            nodes=[node("fetch", "data_fetch", {"limit": 1})],
            edges=[],
        )
        task = asyncio.create_task(first.start(workflow.workflow_id, trace_id="trc_" + "b" * 16, payload={}))
        for _ in range(100):
            await asyncio.sleep(0)
            running = [cp for cp in persistence.checkpoints if cp.instance.status is InstanceStatus.RUNNING]
            if running:
                break
        assert running, "实例开跑后必须先落一条 running 检查点"
        instance_id = running[0].instance.instance_id
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()

        second = make_engine(persistence)
        stats = await second.restore_persisted_state()
        assert stats["instances"] == 1 and stats["interrupted"] == 1

        restored = second.instance_detail(instance_id)
        assert restored is not None
        assert restored["status"] == "failed"
        assert restored["interrupted"] is True and restored["recovered"] is True
        assert "服务重启" in restored["error"]
        assert all(item["state"] == "cancelled" for item in restored["nodes"])

        # 中断是终态事实且已回写：下一次重启不再把它当成可恢复实例。
        third = make_engine(persistence)
        assert (await third.restore_persisted_state())["instances"] == 0

    async def test_等待账与节点状态不一致时显式中断(self) -> None:
        persistence = FakePersistence()
        first = make_engine(persistence)
        workflow = await first.create_definition(
            name="坏等待流程",
            nodes=[node("sign", "human_review", {"prompt": "签", "options": ["approve"]})],
            edges=[],
        )
        detail = await first.start(workflow.workflow_id, trace_id="trc_" + "d" * 16, payload={})
        instance_id = detail["instance_id"]
        broken = persistence.checkpoints[-1]
        persistence.checkpoints.append(
            InstanceCheckpoint(
                instance=broken.instance,
                runtime=broken.runtime.model_copy(update={"pending_waits": {}}),
            )
        )

        second = make_engine(persistence)
        stats = await second.restore_persisted_state()
        assert stats["instances"] == 1 and stats["interrupted"] == 1
        restored = second.instance_detail(instance_id)
        assert restored is not None and restored["status"] == "failed"
        assert "不一致" in restored["error"]

    async def test_运行中改图与画布位置随检查点恢复(self) -> None:
        persistence = FakePersistence()
        first = make_engine(persistence)
        workflow = await first.create_definition(
            name="改图流程",
            nodes=[
                node("sign", "human_review", {"prompt": "签", "options": ["approve"]}),
                node("after", "delay", {"seconds": 0}),
            ],
            edges=[{"source": "sign", "target": "after", "condition": "approve"}],
        )
        detail = await first.start(workflow.workflow_id, trace_id="trc_" + "c" * 16, payload={})
        instance_id = detail["instance_id"]
        await first.update_node_config(instance_id, "after", {"seconds": 0})
        await first.insert_node(
            instance_id,
            after="sign",
            node=node("extra", "delay", {"seconds": 0}),
            position={"x": 10, "y": 20},
        )

        second = make_engine(persistence)
        await second.restore_persisted_state()
        restored = second.instance_detail(instance_id)
        assert restored is not None
        graph = restored["graph"]
        assert {item["node_id"] for item in graph["nodes"]} == {"sign", "after", "extra"}
        assert graph["positions"]["extra"] == {"x": 10.0, "y": 20.0}
        edges = {(edge["source"], edge["target"], edge["condition"]) for edge in graph["edges"]}
        assert edges == {("sign", "extra", ""), ("extra", "after", "approve")}
