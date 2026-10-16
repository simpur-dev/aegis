"""记忆服务测试（§4.3 / D5）：共享命名空间读写/治理 + 有界 + 四类门面降级。"""

from __future__ import annotations

from aegis.domain.messages import now_iso
from aegis.services.memory.memory import AgentMemoryRecord, InMemoryAgentMemory, MemoryService, new_memory_record_id


async def _mem(mem: InMemoryAgentMemory, kind: str, content: str, *, region: str | None = "540121", agent: str = "assess.x") -> str:
    return await mem.write(
        AgentMemoryRecord(
            record_id=new_memory_record_id(), agent_id=agent, kind=kind, content=content, region_code=region, valid_at=now_iso()
        )
    )


class TestSharedNamespace:
    async def test_write_and_search_newest_first(self) -> None:
        mem = InMemoryAgentMemory()
        await _mem(mem, "assessment", "第一条")
        await _mem(mem, "assessment", "第二条")
        found = await mem.search(kind="assessment", limit=5)
        assert [r.content for r in found] == ["第二条", "第一条"], "回注要新在前"

    async def test_filters_by_kind_region_agent(self) -> None:
        mem = InMemoryAgentMemory()
        await _mem(mem, "assessment", "研判A", region="540121")
        await _mem(mem, "lesson", "教训B", region="540121")
        await _mem(mem, "assessment", "他区C", region="540200")
        assert [r.content for r in await mem.search(kind="assessment", region_code="540121")] == ["研判A"]
        assert [r.content for r in await mem.search(agent_id="assess.x")] == ["他区C", "教训B", "研判A"]

    async def test_bounded_evicts_oldest_and_counts(self) -> None:
        mem = InMemoryAgentMemory(capacity=2)
        for i in range(3):
            await _mem(mem, "lesson", f"L{i}")
        assert mem.evicted == 1
        assert [r.content for r in await mem.search(kind="lesson", limit=5)] == ["L2", "L1"]

    async def test_delete_governance(self) -> None:
        mem = InMemoryAgentMemory()
        rid = await _mem(mem, "assessment", "可删除")
        assert await mem.delete(rid) is True
        assert await mem.delete("mem_missing") is False
        assert await mem.search(kind="assessment") == []


class TestMemoryService:
    async def test_shared_roundtrip_and_disagreement_flag(self) -> None:
        svc = MemoryService(InMemoryAgentMemory())
        rid = await svc.write_shared(
            kind="disagreement",
            agent_id="assess.a",
            record_id=new_memory_record_id(),
            content="双盲分歧",
            region_code="540121",
            disagreement=True,
            valid_at=now_iso(),
        )
        rows = await svc.read_shared(kind="disagreement")
        assert rows[0].record_id == rid and rows[0].disagreement is True
        assert await svc.forget(rid) is True

    async def test_read_categories把没接_没命中_读挂了分成三种事实(self) -> None:
        """§4.3 的纪律落在字段上：这三件事都回 `[]` 就等于在骗读的人。

        旧契约（三类各一张列表）正是那枚骗人的形状——`semantic=[]` 既可能是"检索腿挂了"，
        也可能是"这台压根没装检索腿"，而两者的处置动作完全相反（修 vs 装）。
        """

        class OkProvider:
            async def recall(self, query, *, region_code=None, hazard_type=None, limit=3):
                return [{"id": "case:1"}]

        class EmptyProvider:
            async def recall(self, query, *, region_code=None, hazard_type=None, limit=3):
                return []

        class BoomProvider:
            async def recall(self, query, *, region_code=None, hazard_type=None, limit=3):
                raise RuntimeError("图谱挂了")

        svc = MemoryService(InMemoryAgentMemory(), episodic=OkProvider(), semantic=BoomProvider(), knowledge=EmptyProvider())
        result = await svc.read_categories("泥石流", region_code="540121")
        assert result["episodic"] == {"connected": True, "items": [{"id": "case:1"}], "note": ""}
        assert result["semantic"]["connected"] is True and "RuntimeError" in result["semantic"]["note"]
        assert result["semantic"]["items"] == [], "降级那一格仍要回空条目，但不能没有原因"
        assert result["knowledge"]["connected"] is True and result["knowledge"]["items"] == []
        assert "没命中" in result["knowledge"]["note"], "接了但没命中，得与『没接』分得开"

    async def test_未接入的那一类连着也要说没接(self) -> None:
        svc = MemoryService(InMemoryAgentMemory(), episodic=None, semantic=None, knowledge=None)
        result = await svc.read_categories("泥石流")
        assert {name: value["connected"] for name, value in result.items()} == {
            "episodic": False,
            "semantic": False,
            "knowledge": False,
        }
        assert all("未接入" in str(value["note"]) for value in result.values()), result
