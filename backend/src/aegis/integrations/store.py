"""存储面：构造不建连、启动期建池与迁移；连不上就退回“读视图照常 + 写侧缓冲重试”，故障进状态行。（P2 纯搬运：函数体逐字未改。）"""

from __future__ import annotations

from aegis.config import Settings, get_settings
from aegis.integrations.shared import log
from aegis.integrations.state import IntegrationState, StoreBundle
from aegis.storage.store import PlatformStore


def build_store(settings: Settings | None = None, *, read_model: PlatformStore | None = None) -> StoreBundle:
    """按配置组装存储面。

    构造 `PostgresStore` 不建连接（惰性建池），所以没装 `[postgres]` extra 或库不可达时这里不抛错
    ——连通性判定留给 `start_store`。但缺 DSN 是配置错误，构造阶段就报：
    让它静默退化成"只有内存视图"的部署，比开不起来难查一个量级。
    """
    cfg = settings or get_settings()
    read = read_model or PlatformStore()

    if cfg.store_backend != "postgres":
        return StoreBundle(store=read, state=IntegrationState(name="store", enabled=True, driver="memory"))

    from aegis.persistence.postgres import PostgresStore  # 延迟导入：默认部署不必带 asyncpg

    store = PostgresStore(
        dsn=cfg.pg_dsn or None,
        settings=cfg,
        read_model=read,
        pool_min_size=cfg.pg_pool_min_size,
        pool_max_size=cfg.pg_pool_max_size,
    )
    return StoreBundle(
        store=store,
        durability=store,
        state=IntegrationState(
            name="store",
            enabled=True,
            driver="postgres",
            # 只暴露脱敏后的目标：DSN 里的口令绝不进状态接口与日志。
            detail={"target": store.dsn_target, "migrations_on_start": cfg.pg_apply_migrations_on_start},
        ),
    )


async def start_store(bundle: StoreBundle, settings: Settings | None = None) -> IntegrationState:
    """建池并按需应用迁移；失败时保留读视图继续运行，把故障写进状态而不是让进程起不来。"""
    cfg = settings or get_settings()
    if bundle.durability is None:
        return bundle.state

    store = bundle.durability
    detail: dict[str, object] = dict(bundle.state.detail)
    try:
        await store.connect()
    except Exception as exc:
        detail["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        detail["degraded"] = "read_model_only"
        log.warning("持久层连接失败，降级为内存读视图 + 缓冲重试", extra={"target": detail.get("target"), "err": detail["error"]})
        return IntegrationState(name="store", enabled=True, driver="postgres", detail=detail)

    if cfg.pg_apply_migrations_on_start:
        try:
            detail["migrations_applied"] = len(await store.migrate())
        except Exception as exc:
            # 迁移没跑成意味着表结构不可信：继续写会静默丢列，所以退回"读视图照常"的口径，
            # 写侧的失败与丢弃由缓冲自己计数（bounded + drop-oldest），不在这里越权拆它的内部状态。
            detail["error"] = f"MigrationError: {str(exc)[:200]}"
            detail["degraded"] = "read_model_only"
            log.warning("迁移未应用，落库路径不可信", extra={"err": detail["error"]})
            return IntegrationState(name="store", enabled=True, driver="postgres", detail=detail)

    detail["connected"] = True
    log.info("持久层已接入", extra={"target": detail.get("target")})
    return IntegrationState(name="store", enabled=True, driver="postgres", detail=detail)


async def stop_store(bundle: StoreBundle, *, grace_ms: float = 2_000.0) -> None:
    if bundle.durability is None:
        return
    try:
        await bundle.durability.close(grace_ms=grace_ms)
    except Exception as exc:  # 关停尽力而为：这里抛错会让整条优雅退出半途而废
        log.warning("持久层关停异常", extra={"err": type(exc).__name__})
