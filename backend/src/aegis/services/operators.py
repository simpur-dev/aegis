"""席位服务：可读性判定、创建冲突、种子导入。

不在这里判"谁能做什么动作"——那是 `services/assistant/authorize.py` 的事（P1）。
本层只回答三件事：席位台账在不在、这个人是谁、怎么把人写进去。
"""

from __future__ import annotations

from aegis.domain.operator import Operator
from aegis.storage.store import SupportsOperators


class OperatorUnavailableError(RuntimeError):
    """没有持久层就没有席位台账：内存形态不假装能写（与建议台账同口径）。"""


class OperatorConflictError(RuntimeError):
    """同一个 operator_id 已经存在：创建是创建，覆盖是覆盖，两件事不混。"""


class OperatorService:
    def __init__(self, ledger: SupportsOperators | None) -> None:
        self._ledger = ledger

    @property
    def available(self) -> bool:
        return self._ledger is not None

    def _require(self) -> SupportsOperators:
        if self._ledger is None:
            raise OperatorUnavailableError("席位台账不可用：当前存储后端没有 operators 面")
        return self._ledger

    async def list_operators(self, *, active_only: bool = True, limit: int = 200) -> list[Operator]:
        return await self._require().list_operators(active_only=active_only, limit=limit)

    async def get(self, operator_id: str) -> Operator | None:
        return await self._require().get_operator(operator_id)

    async def create(self, operator: Operator) -> Operator:
        created = await self._require().create_operator(operator)
        if created is None:
            raise OperatorConflictError(f"席位已存在：{operator.operator_id}")
        return created

    async def seed(self, operators: list[Operator]) -> int:
        """种子导入按 upsert 语义：同一份清单跑多次结果一致。"""
        ledger = self._require()
        for operator in operators:
            await ledger.upsert_operator(operator)
        return len(operators)
