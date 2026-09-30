"""向量检索：余弦距离 top-k + 结构化过滤。

给后续检索模块用的**纯查询函数**：只返回 (id, score, payload)，不判断"该不该召回"、
不做重排、不做灾种推理 —— 那些是业务规则，属于调用方。

过滤条件按 `args` 追加顺序生成 `$n` 占位符，嵌入向量本身也走占位符（由 pgvector 编解码器编码），
因此任何取值都不会出现在 SQL 文本里。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aegis.persistence.errors import MappingError, VectorArgumentError, VectorEmbeddingError
from aegis.persistence.rows import EMBEDDING_DIM, validate_embedding

MAX_K = 500


def _checked_vector(embedding: Sequence[float], dim: int) -> list[float]:
    """rows 层的映射拒绝（MappingError）在向量检索边界改抛专属类型：两者恢复动作不同。"""
    try:
        return validate_embedding(embedding, dim=dim)
    except MappingError as exc:
        raise VectorEmbeddingError(exc.message, detail=exc.detail) from exc


# 连接句柄只用到 fetch，不做类型绑定以免把驱动依赖带进纯查询模块
Connection = Any


@dataclass(frozen=True, slots=True)
class VectorHit:
    """一条召回结果：score 为余弦相似度（1.0 全同，越小越不相关）。"""

    id: str
    score: float
    payload: dict[str, Any]


def check_k(k: int) -> int:
    if k <= 0:
        raise VectorArgumentError("k 必须为正", detail={"k": k})
    if k > MAX_K:
        raise VectorArgumentError("k 超出单次检索上限", detail={"k": k, "max": MAX_K})
    return int(k)


def check_score_threshold(value: float | None) -> float | None:
    if value is None:
        return None
    if not -1.0 <= value <= 1.0:
        raise VectorArgumentError("score 阈值取值区间为 [-1, 1]", detail={"threshold": value})
    return float(value)


def build_top_k(
    embedding: Sequence[float],
    *,
    k: int,
    hazard_type: str | None = None,
    region_code: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    score_threshold: float | None = None,
    dim: int = EMBEDDING_DIM,
) -> tuple[str, list[Any]]:
    """构造余弦 top-k 查询：返回 (SQL, 参数表)。"""
    vector = _checked_vector(embedding, dim)
    top_k = check_k(k)
    threshold = check_score_threshold(score_threshold)
    if since is not None and since.tzinfo is None:
        raise VectorArgumentError("since 必须带时区")
    if until is not None and until.tzinfo is None:
        raise VectorArgumentError("until 必须带时区")
    if since is not None and until is not None and until <= since:
        raise VectorArgumentError("时间窗逆序或为空窗：until 必须晚于 since", detail={"since": str(since), "until": str(until)})

    args: list[Any] = [vector]
    where = ["embedding IS NOT NULL"]
    if threshold is not None:
        args.append(threshold)
        where.append(f"1 - (embedding <=> ${len(args)}::vector) >= ${len(args)}")
    if hazard_type:
        args.append(hazard_type)
        where.append(f"hazard_type = ${len(args)}")
    if region_code:
        args.append(region_code)
        where.append(f"region_code = ${len(args)}")
    if since is not None:
        args.append(since)
        where.append(f"created_at >= ${len(args)}")
    if until is not None:
        args.append(until)
        where.append(f"created_at < ${len(args)}")
    args.append(top_k)
    sql = (
        "SELECT task_unit_id, hazard_type, region_code, event_id, objective, created_at,\n"
        "       1 - (embedding <=> $1::vector) AS score\n"
        "FROM standardized_task_units\n"
        f"WHERE {' AND '.join(where)}\n"
        "ORDER BY embedding <=> $1::vector\n"
        f"LIMIT ${len(args)}\n"
    )
    return sql, args


def _to_hit(row: Any) -> VectorHit:
    payload = {
        "hazard_type": row["hazard_type"],
        "region_code": row["region_code"],
        "event_id": row["event_id"],
        "objective": row["objective"],
        "created_at": row["created_at"],
    }
    return VectorHit(id=str(row["task_unit_id"]), score=float(row["score"]), payload=payload)


async def search_top_k(
    conn: Connection,
    embedding: Sequence[float],
    *,
    k: int = 10,
    hazard_type: str | None = None,
    region_code: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    score_threshold: float | None = None,
    dim: int = EMBEDDING_DIM,
) -> list[VectorHit]:
    """余弦 top-k 检索；候选集为空（含被过滤到空）时返回空表，不作为异常。"""
    sql, args = build_top_k(
        embedding,
        k=k,
        hazard_type=hazard_type,
        region_code=region_code,
        since=since,
        until=until,
        score_threshold=score_threshold,
        dim=dim,
    )
    return [_to_hit(row) for row in await conn.fetch(sql, *args)]
