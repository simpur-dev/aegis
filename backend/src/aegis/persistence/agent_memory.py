"""共享记忆（`agent_memory`，§4.3 / D5）的 PostgreSQL 侧实现。

单一真源：一行记忆的形状由 `domain.messages.AgentMemoryRecord` 与 `sql/007_agent_memory.sql`
共同定义，本模块只做翻译，不另立一套字段口径，也不自己算任何"相关性"——相似度交给 pgvector
的余弦距离，向量没算成的行**不参与**向量检索（宁可少给几条，也不把无向量的行混进相似度序里，
那样得到的是一份看起来像语义命中、实际是时序拼接的结果）。

内存参考实现 `services/memory.py:InMemoryAgentMemory` 与这里对外行为同口径：同样的过滤参数、
同样的"无向量不参与向量序"。差别只在持久性——重启后一边归零、一边还在，而"共享记忆"这句话
只有后者撑得住。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from aegis.domain.messages import AgentMemoryRecord, SharedKind, now_iso, parse_iso
from aegis.persistence.errors import AccuracyArgumentError, QueryArgumentError
from aegis.persistence.rows import EMBEDDING_DIM, validate_embedding

Connection = Any

#: 与 007 DDL 的 CHECK 同源；两处不一致时插入会被库拒绝，而那时报错的人看不到是谁先说错的。
MEMORY_KINDS: tuple[SharedKind, ...] = ("assessment", "disagreement", "lesson")
REGION_PATTERN = re.compile(r"^[0-9A-Z]{6,24}$")
MAX_CONTENT_CHARS = 2_000
MAX_REFS = 32
MAX_LIMIT = 100

#: 由库自己生成的列（默认值/触发侧），不在绑定值里，也不能出现在 ON CONFLICT 的 SET 里。
#: 单列出来是为了让"DDL 有、builder 没有"这件事是一个说得出理由的事实，而不是一处看起来像漏写的差异。
GENERATED_COLUMNS: tuple[str, ...] = ("written_at",)

MEMORY_COLUMNS = (
    "record_id",
    "agent_id",
    "kind",
    "content",
    "region_code",
    "refs",
    "disagreement",
    "valid_at",
    "embedding",
    "embedded_model",
)


def normalize_region(value: str | None) -> str | None:
    """区划码与站点表/真值表同口径（大写 6–24 位字母数字）；空与 None 都按"全局经验"处理。

    小写在这里就归一，而不是丢给库的 CHECK 去拒：调用方传 `540200` 与 `5402 00` 是两回事，
    前者只是没转大写，后者才是坏数据。
    """
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if not REGION_PATTERN.fullmatch(text):
        raise AccuracyArgumentError("共享记忆的 region_code 口径不符（应为 6–24 位大写字母数字）", detail={"region_code": value})
    return text


def insert_params(
    record: AgentMemoryRecord,
    *,
    embedding: Sequence[float] | None = None,
    embedded_model: str = "",
) -> tuple[Any, ...]:
    """把一条记忆读成绑定值：模型已校验过的形状，这里只补齐存储侧的三件事——区域口径、时刻、向量。"""
    if record.kind not in MEMORY_KINDS:
        raise AccuracyArgumentError(f"共享记忆 kind 不在允许集合内：{record.kind}", detail={"allowed": list(MEMORY_KINDS)})
    if len(record.content) > MAX_CONTENT_CHARS:
        raise AccuracyArgumentError(f"共享记忆内容超长（上限 {MAX_CONTENT_CHARS}）", detail={"actual": len(record.content)})
    if len(record.refs) > MAX_REFS:
        raise AccuracyArgumentError(f"共享记忆 refs 过多（上限 {MAX_REFS}）", detail={"actual": len(record.refs)})
    try:
        valid_at = parse_iso(record.valid_at)
    except ValueError as exc:
        raise AccuracyArgumentError(
            f"共享记忆 valid_at 不是合法 ISO 8601：{record.valid_at}", detail={"record_id": record.record_id}
        ) from exc
    if valid_at.tzinfo is None:
        raise AccuracyArgumentError(f"共享记忆 valid_at 缺时区：{record.valid_at}", detail={"record_id": record.record_id})
    vector = validate_embedding(embedding, dim=EMBEDDING_DIM) if embedding is not None else None
    return (
        record.record_id,
        record.agent_id,
        record.kind,
        record.content,
        normalize_region(record.region_code),
        list(record.refs),
        bool(record.disagreement),
        valid_at,
        vector,
        embedded_model if vector is not None else "",
    )


def build_upsert() -> str:
    """按 record_id 幂等 upsert：同一 id 重写就是"修正这条记忆"（§4.3 记忆可治理），不撞主键也不留两份。"""
    placeholders = ", ".join(f"${index + 1}" for index in range(len(MEMORY_COLUMNS)))
    updates = ", ".join(f"{name} = EXCLUDED.{name}" for name in MEMORY_COLUMNS if name != "record_id")
    return (
        f"INSERT INTO agent_memory ({', '.join(MEMORY_COLUMNS)})\n"
        f"VALUES ({placeholders})\n"
        f"ON CONFLICT (record_id) DO UPDATE SET {updates}\n"
    )


def build_search(
    *,
    kind: SharedKind | None = None,
    agent_id: str | None = None,
    region_code: str | None = None,
    limit: int = 5,
    query_embedding: Sequence[float] | None = None,
) -> tuple[str, list[Any]]:
    """返回 (SQL, 参数)。两种排序模式都是事实，不混着说：给了向量就按余弦距离，没给就按时间倒序。"""
    value = max(1, int(limit))
    if value > MAX_LIMIT:
        raise QueryArgumentError("共享记忆单次读取条数超上限", detail={"limit": value, "max": MAX_LIMIT})
    args: list[Any] = []
    where: list[str] = []
    if kind is not None:
        if kind not in MEMORY_KINDS:
            raise AccuracyArgumentError(f"共享记忆 kind 不在允许集合内：{kind}", detail={"allowed": list(MEMORY_KINDS)})
        args.append(kind)
        where.append(f"kind = ${len(args)}")
    if agent_id is not None:
        args.append(agent_id)
        where.append(f"agent_id = ${len(args)}")
    region = normalize_region(region_code)
    if region is not None:
        args.append(region)
        where.append(f"region_code = ${len(args)}")
    if query_embedding is not None:
        vector = validate_embedding(query_embedding, dim=EMBEDDING_DIM)
        args.append(vector)
        where.append("embedding IS NOT NULL")
        clause = f"embedding <=> ${len(args)}::vector"
    else:
        clause = "valid_at DESC"
    sql = "SELECT record_id, agent_id, kind, content, region_code, refs, disagreement, valid_at, embedded_model\n"
    sql += "FROM agent_memory\n"
    if where:
        sql += f"WHERE {' AND '.join(where)}\n"
    sql += f"ORDER BY {clause}\n"
    args.append(value)
    sql += f"LIMIT ${len(args)}"
    return sql, args


def row_to_record(row: Mapping[str, Any]) -> AgentMemoryRecord:
    """库里的行 -> 领域记录。`valid_at` 一律回成带时区的 ISO 串（UTC 毫秒），与平台其它时刻同口径。"""
    refs = row.get("refs") or []
    if isinstance(refs, str):  # jsonb 编解码器在场时是对象；文本口径下也不能静默变成单条怪值。
        import orjson

        refs = orjson.loads(refs)
    return AgentMemoryRecord(
        record_id=str(row["record_id"]),
        agent_id=str(row.get("agent_id") or ""),
        kind=row["kind"],  # 库侧 CHECK 已保证取值合法；不合法就说明有人绕过 DDL，让它响亮失败
        content=str(row["content"]),
        region_code=row.get("region_code"),
        refs=[str(item) for item in refs][:MAX_REFS],
        disagreement=bool(row.get("disagreement")),
        valid_at=_moment_text(row["valid_at"]),
    )


def _moment_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return now_iso(value)


class PostgresAgentMemory:
    """`AgentMemoryStore` 的 PostgreSQL 实现：借 `PostgresStore` 的连接池，不自建第二条通路。"""

    driver = "postgres"

    def __init__(self, store: Any) -> None:
        acquire = getattr(store, "acquire", None)
        if acquire is None:
            raise QueryArgumentError("共享记忆需要一个提供 acquire() 的连接面", detail={"given": type(store).__name__})
        self._store = store

    async def write(self, record: AgentMemoryRecord, *, embedding: Sequence[float] | None = None, embedded_model: str = "") -> str:
        await self._store.connect()
        async with self._store.acquire() as conn:
            await conn.execute(build_upsert(), *insert_params(record, embedding=embedding, embedded_model=embedded_model))
        return record.record_id

    async def search(
        self,
        *,
        kind: SharedKind | None = None,
        agent_id: str | None = None,
        region_code: str | None = None,
        limit: int = 5,
        query_embedding: Sequence[float] | None = None,
    ) -> list[AgentMemoryRecord]:
        sql, args = build_search(kind=kind, agent_id=agent_id, region_code=region_code, limit=limit, query_embedding=query_embedding)
        await self._store.connect()
        async with self._store.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [row_to_record(row) for row in rows]

    async def delete(self, record_id: str) -> bool:
        await self._store.connect()
        async with self._store.acquire() as conn:
            result = await conn.execute("DELETE FROM agent_memory WHERE record_id = $1", record_id)
        return _deleted(result)


def _deleted(result: Any) -> bool:
    """asyncpg 的 execute 回的是状态串（`DELETE 1`）——受影响行数只能从这里读，不猜。"""
    return str(result).rsplit(None, 1)[-1] != "0"


def memory_store_port(store: Any) -> PostgresAgentMemory | None:
    """能力探测：内存视图没有连接面就返回 None，由装配点决定退到哪一档（与 accuracy_replay_port 同一写法）。"""
    return PostgresAgentMemory(store) if getattr(store, "acquire", None) is not None else None
