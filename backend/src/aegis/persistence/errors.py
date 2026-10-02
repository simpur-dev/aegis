"""持久层类型化错误：驱动异常（asyncpg）不外泄到调用侧，也不吞掉——在此收口后原样带上上下文重抛。"""

from __future__ import annotations


class PersistenceError(Exception):
    """本模块所有错误的基类；`detail` 只放可安全落日志的值，绝不包含 DSN 口令。"""

    def __init__(self, message: str, *, detail: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, object] = detail or {}


class DsnError(PersistenceError):
    """连接串不合法或指向了不受支持的驱动/数据库。"""


class NotReadyError(PersistenceError):
    """连接池尚未建立就发起了需要真实连接的调用。"""


class ConnectionFailedError(PersistenceError):
    """建池/取连接失败：弱网与库不可达时的主错误面，交由写缓冲退避重试。"""


class MigrationError(PersistenceError):
    """DDL 应用失败，或已应用的迁移文件发生漂移。"""


class MappingError(PersistenceError):
    """领域记录 -> 行值映射阶段的拒绝：越界 ID、超大载荷、不可能的时序等。"""


class WriteFailedError(PersistenceError):
    """批量写入在重试预算内仍未成功（热路径不受影响，仅计数）。"""


class QueryFailedError(PersistenceError):
    """只读查询失败：与写路径分开，读侧要能向调用方明确表达"没查到"还是"查不了"。

    `caused_by` 保留被包装的驱动异常类型名，便于排障时不必读堆栈也能定位。
    """

    def __init__(self, message: str, *, detail: dict[str, object] | None = None) -> None:
        super().__init__(message, detail=detail)


class ReplayDatasetError(PersistenceError):
    """回放数据集口径不合法：缺字段、类型不符、行号可定位。"""


class QueryArgumentError(PersistenceError):
    """查询参数被拒绝——尚未触达数据库。

    与 QueryFailedError 严格分开：前者是调用方的错（应映射为 4xx / 直接修调用点），
    后者是"查不了"（弱网或库不可达，读侧要降级并可重试）。混淆二者会让上层无法判定该重试还是该改参数。
    """


class GeoArgumentError(QueryArgumentError):
    """空间参数不合法：经纬度越界、半径超限/非正、WKT 非多边形、时间窗逆序或裸时间。"""


class VectorArgumentError(QueryArgumentError):
    """向量检索参数不合法（除维度外的 k / 阈值 / 时间窗问题）。"""


class AccuracyArgumentError(QueryArgumentError):
    """准确率回放参数或标注行不合法：缺 case_id/灾种/区划、裸时间、等级越界、配对窗非法。"""


class VectorEmbeddingError(VectorArgumentError):
    """嵌入向量本身不可用：维度与列定义不符，或含 NaN/Inf。重试同一个向量必然再失败。"""


def describe(exc: BaseException) -> dict[str, object]:
    """把任意底层异常压成可安全记录的 detail（类型名 + 消息首 512 字）。"""
    return {
        "cause_type": type(exc).__name__,
        "cause": str(exc)[:512],
    }
