"""工具面每个 source 的"同一句话"契约（§4.2）：工具复用的是端点那条读路径，就得问得一样。

这一层最容易在悄悄中分叉：source 里多写一个更小的上限、少一句回退，表现都是
"外部客户端经 MCP 查到的与经 REST 查到的不一样"，而两边各自都算"成功返回"。
所以这里的断言全部对着 **provider 收到的实参**，不是对着返回条数——条数在内置预案库上
常常两种问法都给得出东西，抓不出分叉（这条是变异检查实测出来的：第一版用例就是这么假绿的）。

source 是装配点的私有函数：这里直连它们，是因为要看的正是"发出去的那个调用长什么样"，
绕开 server 反而更准（server 侧的形状由 `test_mcp_tools.py` 与 `test_mcp_api.py` 覆盖）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aegis.config import Settings
from aegis.integrations import (
    _mcp_knowledge_source,
    _mcp_memory_categories_source,
    _mcp_memory_search_source,
    _mcp_memory_write_source,
    _mcp_retrieval_source,
    _mcp_telemetry_source,
)


class SpyKnowledge:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def recall(self, query: str, **kwargs: Any) -> list[Any]:
        self.calls.append({"query": query, **kwargs})
        return []


class SpyStore:
    """只够托住 `store.telemetry.query(...)` 这一条读路径的替身。"""

    def __init__(self) -> None:
        self.asked: dict[str, Any] = {}

    @property
    def telemetry(self) -> Any:
        outer = self

        class _Tel:
            def query(self, **kwargs: Any) -> list[Any]:
                outer.asked.update(kwargs)
                return []

        return _Tel()


class SpyMemory:
    def __init__(self) -> None:
        self.read: dict[str, Any] = {}
        self.write: dict[str, Any] = {}
        self.categories: dict[str, Any] = {}
        self.categories_result: dict[str, dict[str, object]] = {
            "episodic": {"connected": True, "items": [{"case_id": "c1"}], "note": ""},
            "semantic": {"connected": True, "items": [], "note": "接了，但这句查询没命中"},
            "knowledge": {"connected": False, "items": [], "note": "该记忆类未接入（装配点没给这条腿）"},
        }
        self.backend = "spy"

    async def read_shared(self, **kwargs: Any) -> list[Any]:
        self.read.update(kwargs)
        return []

    async def write_shared(self, **kwargs: Any) -> str:
        self.write.update(kwargs)
        return "ignored"

    async def read_categories(self, query: str, **kwargs: Any) -> dict[str, dict[str, object]]:
        self.categories.update({"query": query, **kwargs})
        return dict(self.categories_result)


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


class TestCasesSourceMatchesEndpoint:
    def test_只给灾种时按中文名去问_与端点同一句回退(self) -> None:
        """`GET /api/v1/cases/recall` 在 `q` 空时用 `hazard_type.cn` 兜底，工具面必须问同一句。

        少了这句，provider 收到的是空串：内置库上两种问法都能出东西（所以按条数断言抓不到），
        但语义已经不同——一个是"按灾种召回"，一个是"按空查询 + 灾种过滤"。
        """
        spy = SpyKnowledge()
        _run(_mcp_knowledge_source(spy, Settings())({"query": "", "hazard_type": "debris_flow", "region_code": "540121", "limit": 3}))
        assert spy.calls[0]["query"] == "泥石流", spy.calls
        assert spy.calls[0]["hazard_type"] == "debris_flow"

    def test_灾种不在枚举里时当场拒_provider不收一个猜不到的值(self) -> None:
        spy = SpyKnowledge()
        with pytest.raises(ValueError, match="hazard_type"):
            _run(_mcp_knowledge_source(spy, Settings())({"query": "泥石流", "hazard_type": "随便写的灾种", "limit": 3}))
        assert spy.calls == [], "拒了就不该已经把脏值喂给 provider 了"

    def test_预算取的是配置那一个值_不自造第二个(self) -> None:
        spy = SpyKnowledge()
        _run(_mcp_knowledge_source(spy, Settings(knowledge_recall_budget_ms=777))({"query": "a", "limit": 3}))
        assert spy.calls[0]["budget_ms"] == 777


class TestLimitCeilingsMatchEndpoints:
    def test_遥测上限与端点一致(self) -> None:
        """`GET /api/v1/telemetry` 的 `limit` 是 `ge=1, le=5_000`。

        这里曾自己写了 2_000，于是"客户端要 3000 行、工具面静默给 2000 行"——
        两张门面读同一个库却给出不同条数，而两边都算成功返回。
        """
        store = SpyStore()
        _run(_mcp_telemetry_source(store)({"region_code": "540121", "limit": 3_000}))
        assert store.asked["limit"] == 3_000, store.asked
        _run(_mcp_telemetry_source(store)({"region_code": "540121", "limit": 999_999}))
        assert store.asked["limit"] == 5_000, "超上限仍要收敛，但收敛到的是端点那个数"

    def test_记忆上限与端点一致(self) -> None:
        """`GET /api/v1/memory/shared` 是 `le=100`（这里曾写 50）。"""
        memory = SpyMemory()
        _run(_mcp_memory_search_source(memory)({"kind": "assessment", "limit": 90}))
        assert memory.read["limit"] == 90, memory.read
        _run(_mcp_memory_search_source(memory)({"kind": "assessment", "limit": 10_000}))
        assert memory.read["limit"] == 100, memory.read

    def test_检索k的上限与端点一致(self) -> None:
        asked: dict[str, Any] = {}

        class Retrieval:
            async def retrieve(self, query: Any) -> Any:
                asked["k"] = query.k
                asked["text"] = query.text

                class Outcome:
                    docs: tuple[Any, ...] = ()

                    def as_dict(self) -> dict[str, Any]:
                        return {"degraded": "", "legs": []}

                return Outcome()

        _run(_mcp_retrieval_source(Retrieval())({"query": "泥石流", "k": 500}))
        assert asked["k"] == 20, "端点是 le=20，这里也必须收敛到同一个数"


class TestMemoryWriteSource:
    def test_kind白名单与读侧同一套(self) -> None:
        memory = SpyMemory()
        with pytest.raises(ValueError, match="kind"):
            _run(_mcp_memory_write_source(memory)({"kind": "随便", "content": "x"}))
        assert memory.write == {}
        _run(_mcp_memory_write_source(memory)({"kind": "lesson", "content": "x", "region_code": "540121"}))
        assert memory.write["kind"] == "lesson" and memory.write["content"] == "x"

    def test_record_id与valid_at由服务端生成_客户端给的不算(self) -> None:
        """客户端能指定时间线的话，"这条记忆什么时候写的"就不再是平台的事实。"""
        memory = SpyMemory()
        _run(
            _mcp_memory_write_source(memory)(
                {"kind": "assessment", "content": "x", "record_id": "client-chosen", "valid_at": "1999-01-01T00:00:00Z"}
            )
        )
        assert memory.write["record_id"] != "client-chosen", memory.write
        assert not str(memory.write["valid_at"]).startswith("1999"), memory.write


class TestMemoryCategoriesSource:
    """§4.3 四类记忆门面经工具面出去的那一条：三种现场（没接 / 没命中 / 读错）必须还是三种。"""

    def test_每类的状态原样带出_没接的那类被点名(self) -> None:
        """折叠成一张 items 清单就等于把"这条腿没接"说成"没查到相关记忆"——§4.3 明令禁止的那件事。"""
        memory = SpyMemory()
        out = _run(_mcp_memory_categories_source(memory)({"query": "泥石流", "region_code": "540121", "limit": 3}))
        assert out["categories"] == memory.categories_result
        assert out["disconnected"] == ["knowledge"], out
        assert memory.categories["region_code"] == "540121"

    def test_limit上界与端点一致(self) -> None:
        """`GET /api/v1/memory/categories` 是 `limit: le=10`：两条门面读同一个 provider，条数不该差。"""
        memory = SpyMemory()
        _run(_mcp_memory_categories_source(memory)({"query": "泥石流", "limit": 9}))
        assert memory.categories["limit"] == 9, memory.categories
        _run(_mcp_memory_categories_source(memory)({"query": "泥石流", "limit": 10_000}))
        assert memory.categories["limit"] == 10, memory.categories

    def test_空查询当场拒_端点那条min_length在这里同口径(self) -> None:
        """空串会让三条 provider 各回一次"没命中"，看着像读到了东西而实际上谁也没被问。"""
        memory = SpyMemory()
        with pytest.raises(ValueError, match="query"):
            _run(_mcp_memory_categories_source(memory)({"query": "", "limit": 3}))
        assert memory.categories == {}

    def test_超长查询也拒_别让工具面比端点宽松(self) -> None:
        memory = SpyMemory()
        with pytest.raises(ValueError, match="200"):
            _run(_mcp_memory_categories_source(memory)({"query": "泥" * 201}))
        assert memory.categories == {}

    def test_灾种不在枚举里时当场拒_provider不收一个猜不到的值(self) -> None:
        memory = SpyMemory()
        with pytest.raises(ValueError, match="hazard_type"):
            _run(_mcp_memory_categories_source(memory)({"query": "泥石流", "hazard_type": "随便写的灾种"}))
        assert memory.categories == {}
        _run(_mcp_memory_categories_source(memory)({"query": "泥石流", "hazard_type": "debris_flow"}))
        assert memory.categories["hazard_type"] == "debris_flow"
