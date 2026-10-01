"""seekdb（OceanBase seekdb，Apache-2.0）承载混合检索：向量 ANN 与中文 ngram 全文同库同表。

为什么值得把两条腿都搬进去：混合检索的降级面本来由"ONNX 权重在不在、pgvector 连不连得上、
进程内 BM25 有没有预置语料"三件事决定，装配口径很难对外说清。seekdb 一个引擎同时给出
`VECTOR(d)` + HNSW 余弦 ANN 和 `FULLTEXT ... WITH PARSER ngram` 的相关性打分，两腿共用同一份
语料与同一套结构化列，回读一次就拿到正文与凭证字段。

三条口径（都是本机真引擎上量出来的，不是照文档猜的）：

1. **全文解析器必须回读校验**。默认解析器是 `space`（`min_token_size=3`），中文整句成一个
   token，`MATCH ... AGAINST('泥石流')` 对四条案例一律返回 0 分——表建得成、索引建得成、
   查询也不报错，只是永远召不回。所以 `prepare_schema()` 会从 `SHOW CREATE TABLE` 里读回
   解析器与向量索引定义，不是 ngram 就响亮失败。
2. **每次取数各开一条连接**。PyMySQL 连接不是并发安全的，而服务侧两条腿是 `gather` 并发的；
   用一把锁把它们串起来会直接毁掉"总时延趋于 max(腿) 而不是 sum(腿)"这条预算依据。
   本机建连成本是毫秒级，换并发值得，代价写在这里而不是藏在实现里。
3. **值全部走绑定参数**。`doc_id` 允许出现引号与分号（真实编号就带 `-`），拼进 SQL 文本等于
   把注入面交出去。表名不接受外部输入：只允许 `[a-z][a-z0-9_]{0,62}`，其余一律拒绝。
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from typing import Any, Final

from aegis.persistence.rows import EMBEDDING_DIM
from aegis.retrieval.docs import MAX_INPUT_CHARS, KnowledgeDoc
from aegis.retrieval.onnx_io import RetrievalArgumentError, RetrievalError
from aegis.retrieval.port import IndexedRow, IndexFilter

TABLE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

# 列宽从检索层的文本上限反推，避免"DDL 写 2048、语料能到 3000"这种只在插入当天报错的漂移。
CONTENT_WIDTH: Final = max(MAX_INPUT_CHARS, 2_048)
DOC_ID_WIDTH: Final = 64
HAZARD_WIDTH: Final = 32
REGION_WIDTH: Final = 24
SOURCE_WIDTH: Final = 64

DEFAULT_TABLE: Final = "aegis_knowledge_doc"
DEFAULT_NGRAM_TOKEN_SIZE: Final = 2
ANALYZER: Final = "ngram"
DISTANCE: Final = "cosine"


class SeekdbIndexError(RetrievalError):
    """与 seekdb 引擎交互失败。上层把它记成对应腿的降级，而不是抛进预警路径。"""


def _quote_ident(name: str) -> str:
    if not TABLE_PATTERN.fullmatch(name):
        raise RetrievalArgumentError(
            "表名只能是小写字母开头的 [a-z0-9_]，长度 1-63",
            detail={"table": name[:80]},
        )
    return f"`{name}`"


def _vector_literal(vector: Sequence[float], *, dim: int) -> str:
    if len(vector) != dim:
        raise RetrievalArgumentError(
            f"向量维度与索引不一致：期望 {dim}，实得 {len(vector)}",
            detail={"expected": dim, "actual": len(vector)},
        )
    values: list[str] = []
    for item in vector:
        number = float(item)
        if not math.isfinite(number):
            raise RetrievalArgumentError("向量含 NaN/Inf，seekdb 会直接拒绝写入", detail={"value": repr(item)})
        values.append(repr(number))
    return "[" + ",".join(values) + "]"


class SeekdbConfig:
    """连接与索引形态。密码只活在这里和驱动里，永远不进状态面。"""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 2881,
        user: str = "root",
        password: str = "",
        database: str = "test",
        table: str = DEFAULT_TABLE,
        dim: int = EMBEDDING_DIM,
        ngram_token_size: int = DEFAULT_NGRAM_TOKEN_SIZE,
        connect_timeout_ms: int = 3_000,
        query_timeout_ms: int = 3_000,
    ) -> None:
        if dim < 1:
            raise RetrievalArgumentError("dim 必须为正", detail={"dim": dim})
        if port < 1 or port > 65_535:
            raise RetrievalArgumentError("port 必须在 1-65535", detail={"port": port})
        if not 1 <= ngram_token_size <= 8:
            raise RetrievalArgumentError("ngram_token_size 必须在 1-8", detail={"ngram_token_size": ngram_token_size})
        _quote_ident(table)
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.table = table
        self.dim = dim
        self.ngram_token_size = ngram_token_size
        self.connect_timeout_ms = connect_timeout_ms
        self.query_timeout_ms = query_timeout_ms

    def target(self) -> str:
        """对外可展示的端点：host:port + 库表，不含任何凭据。"""
        return f"{self.host}:{self.port}/{self.database}.{self.table}"


class SeekdbKnowledgeIndex:
    """`HybridIndex` 的 seekdb 实现：一条连接工厂 + 三段 SQL。"""

    def __init__(
        self,
        config: SeekdbConfig,
        *,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        self._config = config
        self._connect = connect
        self._analyzer: str | None = None
        self._indexed_docs = 0
        self._last_error: str | None = None

    @property
    def config(self) -> SeekdbConfig:
        return self._config

    # ---------- 与引擎的窄 I/O 面 ----------

    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect()
        try:
            import pymysql
        except ImportError as exc:
            raise SeekdbIndexError(
                "seekdb 检索需要 pymysql 依赖：uv sync --extra seekdb",
                detail={"cause": type(exc).__name__},
            ) from exc
        cfg = self._config
        return pymysql.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
            charset="utf8mb4",
            connect_timeout=max(cfg.connect_timeout_ms / 1000.0, 0.1),
            read_timeout=max(cfg.query_timeout_ms / 1000.0, 0.1),
            write_timeout=max(cfg.query_timeout_ms / 1000.0, 0.1),
            autocommit=False,
        )

    def _run(self, statements: Iterable[tuple[str, Sequence[Any]]], *, fetch: bool) -> list[tuple[Any, ...]]:
        """一次取数 = 一条新连接上跑完这批语句。任何异常都翻译成 `SeekdbIndexError` 并留原因。"""
        connection = self._open()
        rows: list[tuple[Any, ...]] = []
        try:
            with connection.cursor() as cursor:
                for sql, params in statements:
                    cursor.execute(sql, params)
                    if fetch:
                        rows.extend(cursor.fetchall())
            connection.commit()
        except SeekdbIndexError:
            raise
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            raise SeekdbIndexError(f"seekdb 执行失败: {self._last_error}", detail={"cause": type(exc).__name__}) from exc
        finally:
            # 关闭失败不该覆盖已经拿到的结果，也不该把一次成功的取数变成异常。
            with contextlib.suppress(Exception):
                connection.close()
        return rows

    async def _execute(self, *statements: tuple[str, Sequence[Any]]) -> None:
        await asyncio.to_thread(self._run, statements, fetch=False)

    async def _fetch(self, *statements: tuple[str, Sequence[Any]]) -> list[tuple[Any, ...]]:
        return await asyncio.to_thread(self._run, statements, fetch=True)

    # ---------- 建库与灌数 ----------

    def create_table_sql(self) -> str:
        cfg = self._config
        table = _quote_ident(cfg.table)
        return (
            f"CREATE TABLE IF NOT EXISTS {table} (\n"
            "  doc_id VARCHAR(%d) NOT NULL,\n"
            "  content VARCHAR(%d) DEFAULT NULL,\n"
            "  hazard_type VARCHAR(%d) DEFAULT NULL,\n"
            "  region_code VARCHAR(%d) DEFAULT NULL,\n"
            "  source VARCHAR(%d) DEFAULT NULL,\n"
            "  updated_at DATETIME(6) DEFAULT NULL,\n"
            f"  emb VECTOR({cfg.dim}) DEFAULT NULL,\n"
            "  PRIMARY KEY (doc_id),\n"
            f"  FULLTEXT KEY ft_content (content) WITH PARSER {ANALYZER} "
            f"PARSER_PROPERTIES=(ngram_token_size={cfg.ngram_token_size}),\n"
            f"  VECTOR KEY vdx_emb (emb) WITH (distance={DISTANCE}, type=hnsw)\n"
            f") ORGANIZATION INDEX DEFAULT CHARSET = utf8mb4"
        ) % (DOC_ID_WIDTH, CONTENT_WIDTH, HAZARD_WIDTH, REGION_WIDTH, SOURCE_WIDTH)

    async def prepare_schema(self) -> dict[str, object]:
        """建表并把"解析器/向量索引真的生效了吗"回读出来。

        默认 `space` 解析器下中文全文检索会静默返回 0 分（本机实测四条案例全 0），
        表与索引却都建得成——只看"有没有报错"是查不出来的，必须回读定义。
        """
        table = _quote_ident(self._config.table)
        await self._execute((self.create_table_sql(), ()))
        rows = await self._fetch((f"SHOW CREATE TABLE {table}", ()))
        definition = str(rows[0][1]) if rows else ""
        fulltext = next((line.strip() for line in definition.splitlines() if "FULLTEXT" in line), "")
        vector = next((line.strip() for line in definition.splitlines() if "VECTOR KEY" in line), "")
        if f"WITH PARSER {ANALYZER}" not in fulltext:
            raise SeekdbIndexError(
                "seekdb 建出的全文索引不是 ngram 解析器，中文词面召回会静默返回 0 分",
                detail={"fulltext": fulltext[:200]},
            )
        if "VECTOR KEY" not in vector or DISTANCE.upper() not in vector:
            raise SeekdbIndexError(
                "seekdb 建出的表上没有余弦向量索引，密集腿无法做近似最近邻",
                detail={"vector": vector[:200]},
            )
        self._analyzer = f"{ANALYZER}({self._config.ngram_token_size})"
        # 两个索引定义都回传：装配行与在线用例都要能指出"引擎实际建出来的是什么"，
        # 而不是只报告"我们请求了 ngram + cosine"。
        return {
            "analyzer": self._analyzer,
            "fulltext_index": fulltext,
            "vector_index": vector,
            "dim": self._config.dim,
        }

    async def upsert(self, docs: Sequence[KnowledgeDoc], vectors: Sequence[Sequence[float]]) -> int:
        """写入语料与向量。长度不一致直接拒绝：静默丢掉一半语料比报错更难查。"""
        if len(docs) != len(vectors):
            raise RetrievalArgumentError(
                "语料与向量数量不一致",
                detail={"docs": len(docs), "vectors": len(vectors)},
            )
        if not docs:
            return 0
        table = _quote_ident(self._config.table)
        sql = (
            f"INSERT INTO {table} (doc_id, content, hazard_type, region_code, source, updated_at, emb) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE content = VALUES(content), hazard_type = VALUES(hazard_type), "
            "region_code = VALUES(region_code), source = VALUES(source), updated_at = VALUES(updated_at), emb = VALUES(emb)"
        )
        statements: list[tuple[str, Sequence[Any]]] = []
        for doc, vector in zip(docs, vectors, strict=True):
            if len(doc.doc_id) > DOC_ID_WIDTH:
                raise RetrievalArgumentError(
                    f"doc_id 超过列宽 {DOC_ID_WIDTH}",
                    detail={"doc_id": doc.doc_id[:80], "length": len(doc.doc_id)},
                )
            statements.append(
                (
                    sql,
                    (
                        doc.doc_id,
                        doc.text[:CONTENT_WIDTH],
                        (doc.hazard_type or "")[:HAZARD_WIDTH] or None,
                        (doc.region_code or "")[:REGION_WIDTH] or None,
                        (doc.source or "")[:SOURCE_WIDTH] or None,
                        doc.updated_at,
                        _vector_literal(vector, dim=self._config.dim),
                    ),
                )
            )
        await self._execute(*statements)
        self._indexed_docs = len(docs)
        return len(docs)

    async def count(self) -> int:
        table = _quote_ident(self._config.table)
        rows = await self._fetch((f"SELECT COUNT(*) FROM {table}", ()))
        return int(rows[0][0]) if rows else 0

    # ---------- 两条腿 ----------

    def _where(self, filter: IndexFilter) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if filter.hazard_type is not None:
            clauses.append("hazard_type = %s")
            params.append(filter.hazard_type)
        if filter.region_code is not None:
            clauses.append("region_code = %s")
            params.append(filter.region_code)
        if filter.since is not None:
            clauses.append("updated_at >= %s")
            params.append(filter.since)
        if filter.until is not None:
            clauses.append("updated_at <= %s")
            params.append(filter.until)
        return (" AND ".join(clauses), params)

    async def dense_topk(self, vector: Sequence[float], *, limit: int, filter: IndexFilter) -> tuple[IndexedRow, ...]:
        if limit <= 0:
            return ()
        literal = _vector_literal(vector, dim=self._config.dim)
        table = _quote_ident(self._config.table)
        clause, params = self._where(filter)
        sql = (
            f"SELECT doc_id, content, hazard_type, region_code, source, updated_at, "
            f"cosine_distance(emb, %s) AS dist FROM {table} WHERE emb IS NOT NULL"
            + (f" AND {clause}" if clause else "")
            + " ORDER BY dist APPROXIMATE LIMIT %s"
        )
        rows = await self._fetch((sql, [literal, *params, limit]))
        out: list[IndexedRow] = []
        for row in rows:
            score = 1.0 - float(row[6])
            if filter.score_threshold is not None and score < filter.score_threshold:
                continue
            out.append(IndexedRow(doc=_doc_from_row(row), score=score))
        return tuple(out[:limit])

    async def lexical_topk(self, text: str, *, limit: int, filter: IndexFilter) -> tuple[IndexedRow, ...]:
        if limit <= 0 or not text.strip():
            return ()
        table = _quote_ident(self._config.table)
        clause, params = self._where(filter)
        match = "MATCH(content) AGAINST(%s IN NATURAL LANGUAGE MODE)"
        sql = (
            f"SELECT doc_id, content, hazard_type, region_code, source, updated_at, {match} AS score "
            f"FROM {table} WHERE {match}" + (f" AND {clause}" if clause else "") + " ORDER BY score DESC LIMIT %s"
        )
        # 同一个表达式在 SELECT 与 WHERE 各出现一次，绑定参数要按位置给两份。
        rows = await self._fetch((sql, [text, text, *params, limit]))
        out: list[IndexedRow] = []
        for row in rows:
            score = float(row[6])
            if filter.score_threshold is not None and score < filter.score_threshold:
                continue
            out.append(IndexedRow(doc=_doc_from_row(row), score=score))
        return tuple(out[:limit])

    # ---------- 对外状态 ----------

    def status(self) -> dict[str, object]:
        """状态面口径：给端点与库表名，不给用户名与密码。"""
        return {
            "target": self._config.target(),
            "dim": self._config.dim,
            "analyzer": self._analyzer or "未建表",
            "distance": DISTANCE,
            "indexed_docs": self._indexed_docs,
            **({"last_error": self._last_error} if self._last_error else {}),
        }

    async def aclose(self) -> None:
        """没有常驻连接可关：每次取数各开一条，用完即闭。留着这个方法是为了关停序一致。"""
        return None


def _doc_from_row(row: Sequence[Any]) -> KnowledgeDoc:
    return KnowledgeDoc(
        doc_id=str(row[0]),
        text=str(row[1] or ""),
        hazard_type=row[2] or None,
        region_code=row[3] or None,
        source=str(row[4] or ""),
        updated_at=row[5] if isinstance(row[5], datetime) else None,
    )
