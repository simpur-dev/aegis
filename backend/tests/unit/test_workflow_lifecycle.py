"""草稿与发布：revision 冲突、无变化、并发发布、归档、恢复与重命名。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aegis.config import Settings
from aegis.workflow.documents import WorkflowDocument, WorkflowDraft
from aegis.workflow.drafts import DraftService, LineageLocks
from aegis.workflow.engine import WorkflowEngine
from aegis.workflow.errors import WorkflowAppError, WorkflowErrorCode
from aegis.workflow.nodes import WorkflowServices
from aegis.workflow.publishing import PublishingService
from aegis.workflow.store import WorkflowRepository, build_definition

SEED_ID = "wf_" + "a" * 12


class FakeClock:
    def __init__(self) -> None:
        self.ticks = 0

    def now_iso(self) -> str:
        self.ticks += 1
        return f"2026-01-01T00:00:{self.ticks:02d}.000Z"


class SeqIds:
    def __init__(self) -> None:
        self.count = 100

    def workflow_id(self) -> str:
        self.count += 1
        return f"wf_{self.count:012x}"

    def instance_id(self) -> str:
        return f"wfi_{self.count:012x}"


class Harness:
    def __init__(self) -> None:
        self.repository = WorkflowRepository()
        self.clock = FakeClock()
        self.ids = SeqIds()
        engine = WorkflowEngine(repository=self.repository, services=WorkflowServices(), settings=Settings(env="test"))
        self.locks = LineageLocks()
        self.drafts = DraftService(self.repository, locks=self.locks, clock=self.clock, ids=self.ids)
        self.publishing = PublishingService(
            self.repository,
            self.drafts,
            locks=self.locks,
            ids=self.ids,
            clock=self.clock,
            validate_definition=engine.validate_definition,
        )

    async def seed(self, *, name: str = "泥石流处置", description: str = "") -> str:
        definition = build_definition(
            workflow_id=SEED_ID,
            name=name,
            description=description,
            nodes=[{"node_id": "a", "type": "notify", "config": {"text": "广播"}}],
            edges=[],
        )
        await self.repository.save(definition)
        return definition.lineage_id

    async def edit(self, lineage_id: str, **changes: Any) -> WorkflowDraft:
        draft = await self.drafts.get(lineage_id)
        assert draft is not None
        # 结构变更必须重新走模型校验：model_copy 不会把裸 dict 变成 NodeDef，
        # 会把"发布时校验"这条用例变成序列化期崩溃。
        document = WorkflowDocument.model_validate({**draft.document.model_dump(), **changes})
        return await self.drafts.put(lineage_id, document, base_revision=draft.revision)


@pytest.fixture()
def harness() -> Harness:
    return Harness()


class TestDrafts:
    async def test_首次读取草稿从最新版本投影(self, harness: Harness) -> None:
        lineage = await harness.seed()
        draft = await harness.drafts.get(lineage)
        assert draft is not None
        assert draft.revision == 1
        assert draft.base_workflow_id == SEED_ID
        assert draft.document.lineage_id == lineage
        assert draft.document.nodes[0].node_id == "a"

    async def test_未知_lineage_没有草稿(self, harness: Harness) -> None:
        assert await harness.drafts.get("wf_" + "f" * 12) is None

    async def test_空白草稿自动带_start(self, harness: Harness) -> None:
        draft = await harness.drafts.create("新应急流程")
        assert draft.revision == 1
        assert draft.base_workflow_id is None
        assert [node.type for node in draft.document.nodes] == ["start"]
        assert draft.document.layout.positions == {}

    async def test_空白草稿重名拒绝(self, harness: Harness) -> None:
        await harness.seed(name="已存在")
        with pytest.raises(WorkflowAppError) as caught:
            await harness.drafts.create("已存在")
        assert caught.value.code is WorkflowErrorCode.NAME_CONFLICT

    async def test_revision_不匹配时_409_并给出服务器版本(self, harness: Harness) -> None:
        lineage = await harness.seed()
        draft = await harness.drafts.get(lineage)
        assert draft is not None
        await harness.edit(lineage, description="第一次")
        with pytest.raises(WorkflowAppError) as caught:
            await harness.drafts.put(lineage, draft.document, base_revision=draft.revision)
        assert caught.value.code is WorkflowErrorCode.REVISION_CONFLICT
        assert caught.value.status == 409
        assert caught.value.details["server_revision"] == 2

    async def test_新_lineage_首次写入要求_base_0(self, harness: Harness) -> None:
        document = WorkflowDocument(
            lineage_id="wf_" + "e" * 12,
            name="全新流程",
            nodes=[{"node_id": "start", "type": "start"}],
        )
        with pytest.raises(WorkflowAppError) as caught:
            await harness.drafts.put(document.lineage_id, document, base_revision=1)
        assert caught.value.details["server_revision"] == 0
        saved = await harness.drafts.put(document.lineage_id, document, base_revision=0)
        assert saved.revision == 1

    async def test_document_lineage_与路径不一致被拒(self, harness: Harness) -> None:
        await harness.seed()
        document = WorkflowDocument(
            lineage_id="wf_" + "d" * 12,
            name="错位",
            nodes=[{"node_id": "start", "type": "start"}],
        )
        with pytest.raises(WorkflowAppError) as caught:
            await harness.drafts.put("wf_" + "e" * 12, document, base_revision=0)
        assert caught.value.code is WorkflowErrorCode.VALIDATION_FAILED


class TestPublish:
    async def test_发布产生新版本且草稿_revision_前进(self, harness: Harness) -> None:
        lineage = await harness.seed()
        edited = await harness.edit(lineage, description="改一版")
        stored = await harness.publishing.publish(lineage, base_revision=edited.revision)

        assert stored.version == 2
        assert stored.lineage_id == lineage
        assert stored.description == "改一版"
        current = harness.drafts.peek(lineage)
        assert current is not None
        assert current.revision == edited.revision + 1
        assert current.base_workflow_id == stored.workflow_id
        assert stored.created_at.startswith("2026-01-01T00:00:")

    async def test_版本列表最新在前且标出当前版本(self, harness: Harness) -> None:
        lineage = await harness.seed()
        edited = await harness.edit(lineage, description="改一版")
        await harness.publishing.publish(lineage, base_revision=edited.revision)
        rows = harness.publishing.list_versions(lineage)
        assert [row["version"] for row in rows] == [2, 1]
        assert rows[0]["is_current"] is True
        assert rows[1]["is_current"] is False
        assert rows[0]["content_hash"].startswith("sha256:")

    async def test_内容没变时拒绝空版本(self, harness: Harness) -> None:
        lineage = await harness.seed()
        await harness.edit(lineage, description="改一版")
        draft = harness.drafts.peek(lineage)
        assert draft is not None
        await harness.publishing.publish(lineage, base_revision=draft.revision)
        current = harness.drafts.peek(lineage)
        assert current is not None
        with pytest.raises(WorkflowAppError) as caught:
            await harness.publishing.publish(lineage, base_revision=current.revision)
        assert caught.value.code is WorkflowErrorCode.NO_CHANGES
        assert caught.value.status == 409

    async def test_并发发布只有一路成功(self, harness: Harness) -> None:
        lineage = await harness.seed()
        edited = await harness.edit(lineage, description="并发展开")
        revision = edited.revision

        async def attempt():
            try:
                return ("ok", (await harness.publishing.publish(lineage, base_revision=revision)).version)
            except WorkflowAppError as exc:
                return (exc.code.value, None)

        results = await asyncio.gather(attempt(), attempt())
        assert sorted(result[0] for result in results) == ["ok", "revision_conflict"]
        versions = [item.version for item in harness.repository.versions_of(lineage)]
        assert versions == [1, 2], "两路并发不能各写一版"

    async def test_发布重名_409(self, harness: Harness) -> None:
        await harness.seed(name="甲流程")
        other = await harness.drafts.create("乙流程")
        conflict_document = other.document.model_copy(update={"name": "甲流程"})
        edited = await harness.drafts.put(other.lineage_id, conflict_document, base_revision=other.revision)
        with pytest.raises(WorkflowAppError) as caught:
            await harness.publishing.publish(other.lineage_id, base_revision=edited.revision)
        assert caught.value.code is WorkflowErrorCode.NAME_CONFLICT

    async def test_已发布流程的草稿改名撞上别人_409(self, harness: Harness) -> None:
        lineage = await harness.seed(name="甲流程")
        other = await harness.drafts.create("乙流程")
        await harness.publishing.publish(other.lineage_id, base_revision=other.revision)
        edited = await harness.edit(lineage, name="乙流程")
        with pytest.raises(WorkflowAppError) as caught:
            await harness.publishing.publish(lineage, base_revision=edited.revision)
        assert caught.value.code is WorkflowErrorCode.NAME_CONFLICT
        assert [item.version for item in harness.repository.versions_of(lineage)] == [1], "重名失败不能留下半个版本"

    async def test_归档的_lineage_不允许发布(self, harness: Harness) -> None:
        lineage = await harness.seed()
        latest = harness.repository.latest_by_lineage(lineage)
        assert latest is not None
        await harness.repository.save(latest.model_copy(update={"status": "archived"}))
        edited = await harness.edit(lineage, description="归档后修改")
        with pytest.raises(WorkflowAppError) as caught:
            await harness.publishing.publish(lineage, base_revision=edited.revision)
        assert caught.value.code is WorkflowErrorCode.ARCHIVED
        assert caught.value.status == 409

    async def test_发布时校验节点类型(self, harness: Harness) -> None:
        lineage = await harness.seed()
        edited = await harness.edit(
            lineage,
            nodes=[{"node_id": "a", "type": "unknown_type", "config": {}}],
        )
        with pytest.raises(WorkflowAppError) as caught:
            await harness.publishing.publish(lineage, base_revision=edited.revision)
        assert caught.value.code is WorkflowErrorCode.VALIDATION_FAILED

    async def test_不存在的草稿发布会_404(self, harness: Harness) -> None:
        with pytest.raises(WorkflowAppError) as caught:
            await harness.publishing.publish("wf_" + "f" * 12, base_revision=1)
        assert caught.value.code is WorkflowErrorCode.NOT_FOUND


class TestRevert:
    async def test_恢复历史版本不删除后续版本(self, harness: Harness) -> None:
        lineage = await harness.seed(description="第一版")
        old = harness.repository.get(SEED_ID)
        assert old is not None
        await harness.edit(lineage, description="第二版")
        draft = harness.drafts.peek(lineage)
        assert draft is not None
        await harness.publishing.publish(lineage, base_revision=draft.revision)

        reverted = await harness.publishing.revert_to_draft(lineage, SEED_ID)
        assert reverted.document.description == "第一版"
        assert reverted.base_workflow_id == SEED_ID
        assert [item.version for item in harness.repository.versions_of(lineage)] == [1, 2]
        assert harness.repository.get(SEED_ID) is not None

        published = await harness.publishing.publish(lineage, base_revision=reverted.revision)
        assert published.version == 3
        assert [item.version for item in harness.repository.versions_of(lineage)] == [1, 2, 3]

    async def test_恢复别的_lineage_的版本_404(self, harness: Harness) -> None:
        await harness.seed(name="甲")
        other = await harness.drafts.create("乙")
        with pytest.raises(WorkflowAppError) as caught:
            await harness.publishing.revert_to_draft(other.lineage_id, SEED_ID)
        assert caught.value.code is WorkflowErrorCode.NOT_FOUND


class TestRename:
    async def test_重命名同步版本与草稿(self, harness: Harness) -> None:
        lineage = await harness.seed(name="旧名")
        await harness.drafts.get(lineage)
        definition, draft = await harness.publishing.rename(lineage, "新名")
        assert definition.name == "新名"
        assert [item.name for item in harness.repository.versions_of(lineage)] == ["新名"]
        assert draft is not None and draft.document.name == "新名"
        assert harness.repository.lineage_of("旧名") is None
        assert harness.repository.lineage_of("新名") == lineage

    async def test_重名_409(self, harness: Harness) -> None:
        lineage = await harness.seed(name="甲")
        # 重命名作用于已发布的 lineage（整条版本族一起改）；草稿族的改名在发布时生效。
        other = await harness.drafts.create("丙")
        await harness.publishing.publish(other.lineage_id, base_revision=other.revision)
        with pytest.raises(WorkflowAppError) as caught:
            await harness.publishing.rename(other.lineage_id, "甲")
        assert caught.value.code is WorkflowErrorCode.NAME_CONFLICT
        assert harness.repository.lineage_of("甲") == lineage
        assert harness.repository.lineage_of("丙") == other.lineage_id

    async def test_未打开草稿也能量命名(self, harness: Harness) -> None:
        lineage = await harness.seed(name="未开草稿")
        definition, draft = await harness.publishing.rename(lineage, "直接改名")
        assert definition.name == "直接改名"
        assert draft is None
        assert harness.repository.latest("直接改名") is not None

    async def test_未知_lineage_重命名_404(self, harness: Harness) -> None:
        with pytest.raises(WorkflowAppError) as caught:
            await harness.publishing.rename("wf_" + "f" * 12, "任意名字")
        assert caught.value.code is WorkflowErrorCode.NOT_FOUND
