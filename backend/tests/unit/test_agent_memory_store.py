"""共享记忆存储侧（`persistence/agent_memory.py` + `sql/007_agent_memory.sql`）的口径用例。

这块最会长出两张皮：Python 侧的列名/枚举/长度上限与建表脚本各说各话。症状不是"测试红"，
而是运行时插入被库拒绝——而拒绝的报错发生在某条预警链路上。所以这里的头号用例是
**拿 DDL 文本反查列集与 CHECK 值**：谁改了 builder 忘了改表（或反过来），当场红。

其余各条钉的都是"读出来不能骗人"的口径：区域码归一、缺时区的时刻一律拒、
向量检索只让有向量的行参与、没配嵌入腿时退到时序而不是伪装成语义命中。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aegis.domain.messages import AgentMemoryRecord, new_memory_record_id
from aegis.persistence.agent_memory import (
    GENERATED_COLUMNS,
    MAX_CONTENT_CHARS,
    MAX_REFS,
    MEMORY_COLUMNS,
    MEMORY_KINDS,
    PostgresAgentMemory,
    build_search,
    build_upsert,
    insert_params,
    memory_store_port,
    normalize_region,
)
from aegis.persistence.errors import AccuracyArgumentError, QueryArgumentError
from aegis.storage.store import PlatformStore

DDL = (Path(__file__).resolve().parents[2] / "src" / "aegis" / "persistence" / "sql" / "007_agent_memory.sql").read_text(encoding="utf-8")
VALID = "2026-10-12T03:20:00.000Z"


def _record(**overrides: Any) -> AgentMemoryRecord:
    payload: dict[str, Any] = {
        "record_id": new_memory_record_id(),
        "agent_id": "assess_real01",
        "kind": "assessment",
        "content": "540200 上周三次橙色研判，两次事后回落到蓝色",
        "region_code": "540200",
        "refs": ["rdn_1", "wrn_2"],
        "valid_at": VALID,
    }
    payload.update(overrides)
    return AgentMemoryRecord(**payload)


def _ddl_columns() -> set[str]:
    body = DDL.split("CREATE TABLE IF NOT EXISTS agent_memory (", 1)[1].split(");", 1)[0]
    names: set[str] = set()
    for line in body.splitlines():
        text = line.strip()
        if not text or text.startswith("--") or text.upper().startswith(("PRIMARY KEY", "CONSTRAINT", "CHECK")):
            continue
        head = text.split()[0]
        if head.endswith(","):
            head = head[:-1]
        names.add(head)
    return names


def test_列集与建表脚本逐列对齐() -> None:
    """builder 与 DDL 少一边就会得到"代码以为写了、库拒收"的运行时故障。"""
    assert _ddl_columns() == set(MEMORY_COLUMNS) | set(GENERATED_COLUMNS), "DDL 里的列要么进 builder，要么显式登记为库生成"


def test_kind枚举与表上的CHECK同源() -> None:
    check = DDL.split("kind          text NOT NULL CHECK (", 1)[1].split(")", 1)[0]
    in_ddl = {item.strip().strip("'") for item in check.split("IN (", 1)[1].split(")")[0].split(",")}
    assert in_ddl == set(MEMORY_KINDS), "两处不一致时插入会被库拒绝，而报错的人看不到是谁先说错的"


def test_内容长度上限与表上的CHECK同一个数() -> None:
    assert f"length(content) <= {MAX_CONTENT_CHARS}" in DDL
    assert MAX_CONTENT_CHARS == 2_000, "pydantic 模型的 max_length 也写死 2000，改这里必须三处一起改"


def test_绑定值个数与列序一致() -> None:
    params = insert_params(_record())
    assert len(params) == len(MEMORY_COLUMNS)
    assert params[MEMORY_COLUMNS.index("record_id")] == params[0]
    assert params[MEMORY_COLUMNS.index("region_code")] == "540200"
    assert params[MEMORY_COLUMNS.index("refs")] == ["rdn_1", "wrn_2"]
    moment = params[MEMORY_COLUMNS.index("valid_at")]
    assert isinstance(moment, datetime) and moment.tzinfo is not None
    assert params[MEMORY_COLUMNS.index("embedding")] is None
    assert params[MEMORY_COLUMNS.index("embedded_model")] == ""


def test_upsert按record_id覆盖而不是撞主键() -> None:
    sql = build_upsert()
    assert "ON CONFLICT (record_id) DO UPDATE SET" in sql, "同一 id 重写就是修正这条记忆，不能报错也不能留两份"
    assert "record_id = EXCLUDED.record_id" not in sql, "主键自己不许被覆盖"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("540200", "540200"), (" 540200 ", "540200"), ("54a200", "54A200"), ("", None), (None, None)],
    ids=["原样", "去空白", "归大写", "空串按全局", "None按全局"],
)
def test_区域码归一(raw: str | None, expected: str | None) -> None:
    assert normalize_region(raw) == expected


def test_坏区域码当场拒绝而不是让库去猜() -> None:
    with pytest.raises(AccuracyArgumentError):
        normalize_region("5402 00")


@pytest.mark.parametrize(("moment", "reason"), [(VALID[:-1], "缺时区"), ("昨天下午", "ISO")], ids=["裸时间", "坏格式"])
def test_时刻不带时区或不合法就不落一行记忆(moment: str, reason: str) -> None:
    with pytest.raises(AccuracyArgumentError, match=reason):
        insert_params(_record(valid_at=moment))


def test_refs过多在进库前就挡下() -> None:
    with pytest.raises(AccuracyArgumentError):
        insert_params(_record(refs=[f"r{i}" for i in range(MAX_REFS + 1)]))


def test_未知kind拒() -> None:
    record = _record()
    object.__setattr__(record, "__dict__", {**record.model_dump(), "kind": "guess"})  # 绕过 pydantic 造一个坏值
    with pytest.raises(AccuracyArgumentError, match="guess"):
        insert_params(record)


def test_没有向量就不成语义检索() -> None:
    sql, args = build_search(query_embedding=[0.1] * 1024, kind="lesson", region_code="540200", limit=3)
    assert "embedding IS NOT NULL" in sql, "没算成向量的行混进相似度序，得到的是一份看着像语义命中的时序拼接"
    assert "ORDER BY embedding <=> $3::vector" in sql
    assert args == ["lesson", "540200", [0.1] * 1024, 3], "参数位与 $n 必须一一对上，错位就是拿区域码去比向量"


def test_没有查询词就按时序() -> None:
    sql, args = build_search(agent_id="assess_real01")
    assert "ORDER BY valid_at DESC" in sql and "embedding" not in sql.split("ORDER BY", 1)[1]
    assert args == ["assess_real01", 5]


def test_单次读取条数有上限() -> None:
    with pytest.raises(QueryArgumentError):
        build_search(limit=101)


def test_内存视图没有连接面就没有这个端口() -> None:
    assert memory_store_port(PlatformStore()) is None


class _StubEmbedder:
    """确定性 1024 维嵌入替身：只回答"有没有向量可用"，不冒充语义质量。"""

    model_id = "stub-1024"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 1_023 for _ in texts]


class _RecordingConn:
    def __init__(self, *, rows: list[dict[str, Any]] | None = None, status: str = "INSERT 0 1") -> None:
        self.sql: list[str] = []
        self.args: list[tuple[Any, ...]] = []
        self._rows = rows or []
        self._status = status

    async def execute(self, sql: str, *args: Any) -> str:
        self.sql.append(sql)
        self.args.append(args)
        return self._status

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.sql.append(sql)
        self.args.append(args)
        return self._rows


class _FakeStore:
    """只给 `connect()` + `acquire()` 两件事，够把实现体与真库的接缝走通。"""

    def __init__(self, conn: _RecordingConn) -> None:
        self._conn = conn
        self.connects = 0

    async def connect(self) -> None:
        self.connects += 1

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_RecordingConn]:
        yield self._conn


@pytest.fixture
def client_store() -> _FakeStore:
    return _FakeStore(_RecordingConn())


@pytest.mark.asyncio
async def test_写一条记忆走的就是那个builder(client_store: _FakeStore) -> None:
    conn = client_store._conn
    memory = PostgresAgentMemory(client_store)
    record = _record()
    assert await memory.write(record, embedding=[0.5] * 1_024, embedded_model="bge-m3-int8") == record.record_id
    assert conn.sql == [build_upsert()], "实现体绕开 builder 自己拼 SQL，就等于开了第二条口径"
    assert conn.args[0][MEMORY_COLUMNS.index("embedding")] is not None
    assert conn.args[0][MEMORY_COLUMNS.index("embedded_model")] == "bge-m3-int8"


@pytest.mark.asyncio
async def test_没有连接面的store构造时就报错() -> None:
    with pytest.raises(QueryArgumentError):
        PostgresAgentMemory(PlatformStore())


@pytest.mark.asyncio
async def test_读回来的行仍是同一形状() -> None:
    row = {
        "record_id": "mem_1",
        "agent_id": "feedback_real01",
        "kind": "lesson",
        "content": "蓝色预警后 2h 泥位回落",
        "region_code": "540121",
        "refs": ["wrn_9"],
        "disagreement": True,
        "valid_at": datetime(2026, 10, 11, 1, 2, 3, 456000, tzinfo=UTC),
        "embedded_model": "bge-m3-int8",
    }
    conn = _RecordingConn(rows=[row])
    records = await PostgresAgentMemory(_FakeStore(conn)).search(kind="lesson", limit=5)
    assert len(records) == 1
    back = records[0]
    assert back.record_id == "mem_1" and back.disagreement is True and back.refs == ["wrn_9"]
    assert back.valid_at.endswith(("+00:00", "Z")), "回读的时刻必须自带时区，否则下一次按窗口检索就平移八小时"


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "expected"), [("DELETE 1", True), ("DELETE 0", False)], ids=["删到", "没删到"])
async def test_删除按受影响行数说话(status: str, expected: bool) -> None:
    conn = _RecordingConn(status=status)
    assert await PostgresAgentMemory(_FakeStore(conn)).delete("mem_1") is expected


@pytest.mark.asyncio
async def test_进程内后端与库侧同口径_无向量的行不参与向量序() -> None:
    from aegis.services.memory import InMemoryAgentMemory

    store = InMemoryAgentMemory()
    plain = _record(record_id="mem_plain", content="没有向量的一条")
    embedded = _record(record_id="mem_vec", content="有向量的一条")
    await store.write(plain)
    await store.write(embedded, embedding=[1.0] + [0.0] * 1_023)
    hit = await store.search(query_embedding=[1.0] + [0.0] * 1_023, limit=5)
    assert [record.record_id for record in hit] == ["mem_vec"], "混进无向量的行就等于把时序伪装成语义命中"
    assert [record.record_id for record in await store.search(limit=5)] == ["mem_vec", "mem_plain"], "无查询词时新在前"


@pytest.mark.asyncio
async def test_同id重写是修正不是追加第二条_与库侧upsert同口径() -> None:
    """Postgres 侧靠 `ON CONFLICT (record_id) DO UPDATE`；这一档如果只 append，
    "治理"在内存部署下就变成"攒出两句互相矛盾的经验"，而调用方看不出差别。"""
    from aegis.services.memory import InMemoryAgentMemory

    store = InMemoryAgentMemory()
    await store.write(_record(record_id="mem_x", content="第一句"))
    await store.write(_record(record_id="mem_x", content="改过的第二句"))
    found = await store.search(limit=10)
    assert [record.record_id for record in found] == ["mem_x"], "同一 id 只能有一条"
    assert found[0].content == "改过的第二句"


def test_装配点按存储能力挑后端(client_store: Any) -> None:
    """§4.3 的"共享"能不能成立，取决于装配点有没有挑这一档库后端：
    挑错了不会报错，只会得到一份"每次都从空记忆开始"的门面——所以这里直接量装配。"""
    from aegis.integrations import build_memory

    service, state = build_memory(store=client_store)
    assert state.name == "memory" and state.driver == "postgres", "接得上库却挑了进程内，等于每次重启都失忆"
    assert isinstance(service.store, PostgresAgentMemory)
    assert service.embedder_ready is False and service.stats["嵌入失败"] == 0


def test_没有连接面的存储只能退到进程内并且如实标出来() -> None:
    from aegis.integrations import build_memory

    service, state = build_memory(store=PlatformStore())
    assert state.driver == "memory", "退档本身没问题，说不清退到哪一档才是问题"
    assert state.detail.get("embedding") == "absent"
    assert service.store.capacity == 1_000, "进程内这一档的上限必须报得出来，不然满了丢最旧没人看得见"


def test_嵌入腿在场时装配点把模型名带进门面() -> None:
    from aegis.integrations import build_memory

    class _Retrieval:
        embedder = _StubEmbedder()

    service, state = build_memory(store=None, retrieval=_Retrieval())
    assert service.embedder_ready is True and service.embedded_model == "stub-1024"
    assert "embedding" not in state.detail, "有嵌入腿时不该再报 absent"


def test_嵌入腿缺位时门面如实报未就绪() -> None:
    from aegis.services.memory import InMemoryAgentMemory, MemoryService

    service = MemoryService(InMemoryAgentMemory())
    assert service.embedder_ready is False and service.backend == "memory"
    assert service.evicted == 0, "Postgres 侧没有淘汰一说，这里如实 0，不拿 None 让人去猜"


@pytest.mark.asyncio
async def test_嵌入失败只降级不断链并且计数() -> None:
    from aegis.services.memory import InMemoryAgentMemory, MemoryService

    class _Boom:
        model_id = "boom"

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("权重没装载")

    service = MemoryService(InMemoryAgentMemory(), embedder=_Boom(), embedded_model="boom", backend="memory")
    record_id = await service.write_shared(
        kind="assessment", agent_id="assess_real01", content="一条研判", record_id=new_memory_record_id(), valid_at=VALID
    )
    assert record_id
    assert service.stats["嵌入失败"] == 1 and service.stats["写入"] == 1, "算不出向量是事实，但这条记忆必须仍然落下"
    assert service.embedder_ready is True, "腿是在的，只是这一次算失败——两件事不能混成一个布尔"


class _Case:
    """长得像 `CaseMatch`：门面要的是 `planning_brief()` 那份映射，不是对象本身。"""

    def __init__(self, case_id: str) -> None:
        self.case_id = case_id

    def planning_brief(self) -> dict[str, object]:
        return {"case_id": self.case_id}


class _KnowledgeLeg:
    def __init__(self, *, primary: object | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        if primary is not None:
            self.primary = primary  # 只有降级链（即配了图谱 URI）才有这一头

    async def recall(self, query: str, **kw: object) -> list[_Case]:
        self.calls.append({"query": query, **kw})
        return [_Case("C-1")]


class _GraphitiLeg:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def recall(self, query: str, **kw: object) -> list[_Case]:
        self.calls.append({"query": query, **kw})
        return [_Case("G-9")]


class _Doc:
    def as_reference(self) -> dict[str, object]:
        return {"doc_id": "D-1"}


class _Outcome:
    def __init__(self) -> None:
        self.docs = [_Doc()]
        self.degradations: list[object] = []


class _RetrievalLeg:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def retrieve(self, query: object) -> _Outcome:
        self.calls.append(query)
        return _Outcome()


def _legs() -> tuple[_KnowledgeLeg, _RetrievalLeg]:
    return _KnowledgeLeg(), _RetrievalLeg()


@pytest.mark.asyncio
async def test_装配点把三类记忆接进门面_图谱缺位时那类如实未接() -> None:
    """§4.3 那句"四类记忆统一门面"此前两头都是空的：装配点一个 provider 都没给，
    而 `read_categories()` 在仓里没有任何生产调用方。这里钉住"接上"与"接不上要说什么"。"""
    from aegis.integrations import build_memory

    knowledge, retrieval = _legs()
    service, state = build_memory(store=None, retrieval=retrieval, knowledge=knowledge)
    assert state.detail["categories"] == {"episodic": "connected", "semantic": "connected", "knowledge": "absent"}
    result = await service.read_categories("泥石流", region_code="540121", hazard_type="debris_flow")
    assert result["episodic"] == {"connected": True, "items": [{"case_id": "C-1"}], "note": ""}
    assert result["semantic"]["items"] == [{"doc_id": "D-1"}]
    assert result["knowledge"]["connected"] is False, "没配图谱就是把内存案例库冒充成图谱命中"


@pytest.mark.asyncio
async def test_配了图谱时知识那一类走图谱而不是案例库() -> None:
    from aegis.integrations import build_memory

    graphiti = _GraphitiLeg()
    knowledge, retrieval = _legs()
    knowledge.primary = graphiti  # type: ignore[attr-defined]
    service, state = build_memory(store=None, retrieval=retrieval, knowledge=knowledge)
    assert state.detail["categories"]["knowledge"] == "connected"
    result = await service.read_categories("冰湖水位")
    assert result["knowledge"]["items"] == [{"case_id": "G-9"}]
    assert graphiti.calls and graphiti.calls[0]["query"] == "冰湖水位"


@pytest.mark.asyncio
async def test_适配器不吞过滤条件与预算() -> None:
    """区域/灾种被丢掉的表现是"跨区经验串进本区预案"，而预算丢了就是 SLA 那格没人守。"""
    from aegis.integrations import build_memory

    knowledge, retrieval = _legs()
    service, _ = build_memory(store=None, retrieval=retrieval, knowledge=knowledge)
    await service.read_categories("泥石流", region_code="540121", hazard_type="debris_flow", limit=2)
    called = knowledge.calls[0]
    assert called["region_code"] == "540121" and called["hazard_type"] == "debris_flow" and called["limit"] == 2
    assert isinstance(called["budget_ms"], float) and called["budget_ms"] > 0
    query = retrieval.calls[0]
    assert getattr(query, "region_code", None) == "540121" and getattr(query, "k", None) == 2
