"""asyncpg / pgvector 的调用面契约测试。

与 test_nats_api_contract.py 同一动机：本层的编解码器注册与向量类型注册都靠关键字参数
调用驱动，参数名写错不会在导入期暴露，只会在真实建连时抛 TypeError —— 而单测若不连库
就永远碰不到。这里用 inspect 把签名钉住，让这类错误在离线单测阶段就红。
"""

from __future__ import annotations

import inspect

import asyncpg
import pytest

try:
    import pgvector.asyncpg as pgvector_asyncpg
except ImportError:  # pragma: no cover - 取决于是否装了 [postgres] 额外依赖
    pgvector_asyncpg = None


def test_set_type_codec_accepts_the_kwargs_we_call_it_with() -> None:
    params = inspect.signature(asyncpg.Connection.set_type_codec).parameters
    assert "schema" in params, "asyncpg 的 schema 参数名已变化：注册 json/geography 编解码器会 TypeError"
    assert "schema_name" not in params, "asyncpg 不存在 schema_name 参数，代码里不得使用"
    assert params["typename"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD

    for name in ("encoder", "decoder", "format"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, f"{name} 必须是关键字参数"


def test_connection_primitives_we_rely_on_exist() -> None:
    for name in ("fetch", "fetchrow", "fetchval", "execute", "executemany", "copy_records_to_table", "set_type_codec"):
        assert hasattr(asyncpg.Connection, name), f"asyncpg.Connection.{name} 已不存在"
    assert callable(asyncpg.create_pool) and callable(asyncpg.connect)


@pytest.mark.skipif(pgvector_asyncpg is None, reason="未安装 pgvector 额外依赖")
def test_pgvector_asyncpg_registers_vector_codec() -> None:
    assert hasattr(pgvector_asyncpg, "register_vector"), "pgvector.asyncpg.register_vector 已改名或缺失"
    assert inspect.iscoroutinefunction(pgvector_asyncpg.register_vector), "register_vector 现为协程：必须 await"
