"""仓储 lineage：按 name 回填、锁内版本号、原子重命名与版本文档快照。"""

from __future__ import annotations

import asyncio

import pytest

from aegis.config import Settings
from aegis.workflow.documents import WorkflowLayout, definition_from_document, document_from_definition, document_hash
from aegis.workflow.engine import WorkflowEngine
from aegis.workflow.nodes import WorkflowServices
from aegis.workflow.store import DuplicateWorkflowName, WorkflowRepository, build_definition


def wf_id(char: str) -> str:
    return "wf_" + char * 12


def definition(workflow_id: str, *, name: str = "泥石流处置", version: int = 1, description: str = ""):
    return build_definition(
        name=name,
        workflow_id=workflow_id,
        version=version,
        description=description,
        nodes=[{"node_id": "a", "type": "notify", "config": {"text": "广播"}}],
        edges=[],
    )


class TestLineageBackfill:
    async def test_同名修订沿用最早版本的_lineage(self) -> None:
        repository = WorkflowRepository()
        first = await repository.save(definition(wf_id("a")))
        second = await repository.save(definition(wf_id("b"), version=2))

        assert second.lineage_id == first.lineage_id == wf_id("a")
        assert [item.version for item in repository.versions_of(first.lineage_id)] == [1, 2]
        assert repository.get(second.workflow_id).lineage_id == first.lineage_id  # type: ignore[union-attr]

    async def test_不同名字各自成族(self) -> None:
        repository = WorkflowRepository()
        first = await repository.save(definition(wf_id("a"), name="甲"))
        second = await repository.save(definition(wf_id("b"), name="乙"))
        assert first.lineage_id != second.lineage_id
        assert len(repository.versions_of(first.lineage_id)) == 1

    async def test_同名同版本不同_id_被拒(self) -> None:
        repository = WorkflowRepository()
        await repository.save(definition(wf_id("a")))
        with pytest.raises(DuplicateWorkflowName):
            await repository.save(definition(wf_id("b")))

    async def test_旧定义没有文档时投影出空版式(self) -> None:
        repository = WorkflowRepository()
        saved = await repository.save(definition(wf_id("a")))
        document = repository.document_of(saved.workflow_id)
        assert document is not None
        assert document.layout.positions == {}
        assert document.name == saved.name

    async def test_内容哈希在保存时补算(self) -> None:
        repository = WorkflowRepository()
        saved = await repository.save(definition(wf_id("a")))
        assert saved.content_hash.startswith("sha256:")


class TestSaveNext:
    async def test_锁内分配连续版本号(self) -> None:
        repository = WorkflowRepository()
        first = await repository.save(definition(wf_id("a")))
        document = repository.document_of(first.workflow_id)
        assert document is not None

        second = definition_from_document(document, workflow_id=wf_id("b"), created_at="2026-01-01T00:00:00.000Z")
        third_document = document.model_copy(update={"description": "第二版"})
        third = definition_from_document(third_document, workflow_id=wf_id("c"), created_at="2026-01-01T00:00:01.000Z")
        saved = await asyncio.gather(
            repository.save_next(second, document=document),
            repository.save_next(third, document=third_document),
        )
        assert sorted(item.version for item in saved) == [2, 3]
        assert [item.version for item in repository.versions_of(first.lineage_id)] == [1, 2, 3]

    async def test_并发_save_next_不撞号(self) -> None:
        repository = WorkflowRepository()
        first = await repository.save(definition(wf_id("a")))
        document = repository.document_of(first.workflow_id)
        assert document is not None

        async def publish(index: int):
            candidate = document.model_copy(update={"description": f"并发-{index}"})
            definition_ = definition_from_document(candidate, workflow_id=f"wf_{index:012x}")
            return await repository.save_next(definition_, document=candidate)

        saved = await asyncio.gather(*(publish(index) for index in range(1, 6)))
        assert sorted(item.version for item in saved) == [2, 3, 4, 5, 6]
        assert len({item.version for item in saved}) == 5

    async def test_名字属于另一条_lineage_时拒绝(self) -> None:
        repository = WorkflowRepository()
        await repository.save(definition(wf_id("a"), name="甲的流程"))
        document = document_from_definition(definition(wf_id("c"), name="甲的流程"))
        candidate = definition_from_document(document, workflow_id=wf_id("d"))
        # 显式指向一条新 lineage，但名字已被甲的 lineage 占用
        candidate = candidate.model_copy(update={"lineage_id": wf_id("e")})
        with pytest.raises(DuplicateWorkflowName):
            await repository.save_next(candidate, document=document)


class TestRename:
    async def test_重命名同步整条_lineage(self) -> None:
        repository = WorkflowRepository()
        first = await repository.save(definition(wf_id("a"), name="旧名"))
        document = document_from_definition(first, layout=WorkflowLayout(positions={"a": {"x": 10, "y": 20}}))
        second = definition_from_document(document, workflow_id=wf_id("b"))
        await repository.save_next(second, document=document)

        renamed = await repository.rename_lineage(first.lineage_id, "新名")

        assert renamed.name == "新名"
        assert repository.latest("旧名") is None
        assert repository.latest("新名") is not None
        assert [item.name for item in repository.versions_of(first.lineage_id)] == ["新名", "新名"]
        assert repository.lineage_of("新名") == first.lineage_id
        assert repository.get(first.workflow_id).name == "新名"  # type: ignore[union-attr]
        stored = repository.document_of(second.workflow_id)
        assert stored is not None and stored.name == "新名" and stored.layout.positions["a"].x == 10
        # 旧版本没有文档快照，改名后哈希也要按投影文档重算，否则与版本列表/读出的文档对不上。
        legacy_document = repository.document_of(first.workflow_id)
        assert legacy_document is not None
        assert repository.get(first.workflow_id).content_hash == document_hash(legacy_document)  # type: ignore[union-attr]

    async def test_重名拒绝且不改旧名(self) -> None:
        repository = WorkflowRepository()
        first = await repository.save(definition(wf_id("a"), name="甲的流程"))
        await repository.save(definition(wf_id("b"), name="乙的流程"))
        with pytest.raises(DuplicateWorkflowName):
            await repository.rename_lineage(first.lineage_id, "乙的流程")
        assert repository.get(first.workflow_id).name == "甲的流程"  # type: ignore[union-attr]
        assert repository.lineage_of("甲的流程") == first.lineage_id

    async def test_重命名同名是空操作(self) -> None:
        repository = WorkflowRepository()
        saved = await repository.save(definition(wf_id("a"), name="不变"))
        renamed = await repository.rename_lineage(saved.lineage_id, "不变")
        assert renamed.workflow_id == saved.workflow_id
        assert repository.versions_of(saved.lineage_id) == [renamed]


class TestArchiveRestoreLineage:
    async def test_v1到v2归档再恢复_lineage_与版本时间不变(self) -> None:
        repository = WorkflowRepository()
        engine = WorkflowEngine(repository=repository, services=WorkflowServices(), settings=Settings(env="test"))
        first = await repository.save(definition(wf_id("a"), name="处置流程"))
        second = await repository.save(definition(wf_id("b"), name="处置流程", version=2, description="第二版"))

        archived = await engine.archive(second.workflow_id)
        restored = await engine.restore(second.workflow_id)

        assert archived.lineage_id == restored.lineage_id == first.lineage_id
        assert restored.created_at == second.created_at
        assert first.created_at == repository.get(first.workflow_id).created_at  # type: ignore[union-attr]
        assert repository.versions("处置流程") == [1, 2]
        assert [item.status for item in repository.versions_of(first.lineage_id)] == ["active", "active"]
        assert repository.latest("处置流程").workflow_id == second.workflow_id  # type: ignore[union-attr]

    async def test_归档旧版本后仍可按_id_读到(self) -> None:
        repository = WorkflowRepository()
        engine = WorkflowEngine(repository=repository, services=WorkflowServices(), settings=Settings(env="test"))
        first = await repository.save(definition(wf_id("a"), name="处置流程"))
        await repository.save(definition(wf_id("b"), name="处置流程", version=2))
        await engine.archive(first.workflow_id)
        assert repository.get(first.workflow_id).status == "archived"  # type: ignore[union-attr]
        assert repository.versions_of(first.lineage_id)[0].version == 1


class TestVersionDocument:
    async def test_仓储文档快照保留版式(self) -> None:
        repository = WorkflowRepository()
        first = await repository.save(definition(wf_id("a")))
        document = document_from_definition(first, layout=WorkflowLayout(positions={"a": {"x": 10, "y": 20}}))
        candidate = definition_from_document(document, workflow_id=wf_id("b"))
        await repository.save_next(candidate, document=document)
        stored = repository.document_of(candidate.workflow_id)
        assert stored is not None and stored.layout.positions["a"].x == 10
        # 旧定义（没有 save_next 文档）仍能读：投影为空版式
        legacy = repository.document_of(first.workflow_id)
        assert legacy is not None and legacy.layout.positions == {}

    async def test_版本列表按版本升序返回(self) -> None:
        repository = WorkflowRepository()
        await repository.save(definition(wf_id("a")))
        await repository.save(definition(wf_id("b"), version=2))
        versions = repository.versions_of(wf_id("a"))
        assert [item.version for item in versions] == [1, 2]


class TestInvariants:
    def test_容量必须为正(self) -> None:
        with pytest.raises(ValueError, match="capacity"):
            WorkflowRepository(capacity=0)

    async def test_容量满时拒绝新定义(self) -> None:
        repository = WorkflowRepository(capacity=1)
        await repository.save(definition(wf_id("a"), name="甲"))
        with pytest.raises(RuntimeError, match="容量上限"):
            await repository.save(definition(wf_id("b"), name="乙"))

    async def test_同一_id_不能改挂另一名字或版本(self) -> None:
        """ID 是版本的身份：允许它换名字会让 `(name, version)` 索引指向一条对不上的记录。"""
        repository = WorkflowRepository()
        await repository.save(definition(wf_id("a"), name="甲"))
        with pytest.raises(DuplicateWorkflowName, match="已属于"):
            await repository.save(definition(wf_id("a"), name="乙"))
        with pytest.raises(DuplicateWorkflowName, match="已属于"):
            await repository.save(definition(wf_id("a"), name="甲", version=2))
