"""双人复核：一个**默认关闭**的高安全配置项，不是审批链。

口径来自升级方案 §3.1 规则 3 与 FR-4.4：
- 默认关（`AEGIS_ASSISTANT_DUAL_REVIEW_ENABLED=false`）时，D5「指令即执行」逐字不变：
  操作者本人的一次确认就执行，助手不额外设审批、也不削减操作者的能力；
- 打开后只对四类不可逆动作生效（注册表里 `ActionSpec.dual_review=True` 的那 10 个）：
  发布预警、批量、改规则、设备控制。读动作、演练、上报、认领都不进这条路；
- 一人一票、两票必须来自**不同席位**且达到角色门槛；两个值班长同时点第二票，
  靠台账的一条条件 UPDATE 抢占，只有一次能成功执行（见 persistence/control_actions.py）。

为什么单独一个模块：票的合法性（角色/是否重复）与票的落库（状态迁移）是两件事，
前者属于策略、后者属于台账。混进 `AssistantService.confirm` 里会让"为什么这个人不能点"
变成一句散在分支里的 if，测试也没法单独钉住。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from aegis.domain.operator import ROLE_RANK, Operator, OperatorRole, role_at_least
from aegis.services.assistant.base import ActionError, ActionSpec

#: 复核票数固定为 2：设计里没有"三票""按等级加票"的形态，别开一个没实现的旋钮。
REVIEW_VOTES = 2
#: 复核席位的下限：`duty` 点击被拒并留痕（FR-4.4）。动作本身若要求 admin，取更严的那个。
_MIN_REVIEW_ROLE = OperatorRole.LEAD


class ReviewLedger(Protocol):
    """只需要台账的一票接口与读回；内存与 PostgreSQL 两套实现同语义。

    读回只用于把"同一人重复投"与"动作已处置/已过期"分成两句不同的话，
    不用于先查后改的并发判定——那仍然由台账的条件 UPDATE 原子完成。
    """

    async def review_action(self, action_id: str, reviewer: dict[str, Any]) -> tuple[dict[str, Any], bool] | None: ...

    async def get_action(self, action_id: str) -> dict[str, Any] | None: ...


class ReviewPolicy:
    """配置驱动的"这个动作要不要复核、谁能投"——唯一判据，不在路由或前端再判一遍。"""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = bool(enabled)

    def votes_required(self, spec: ActionSpec) -> int:
        return REVIEW_VOTES if self.enabled and spec.dual_review else 0

    def minimum_role(self, spec: ActionSpec) -> OperatorRole:
        return spec.required_role if ROLE_RANK[spec.required_role] > ROLE_RANK[_MIN_REVIEW_ROLE] else _MIN_REVIEW_ROLE

    def authorize(self, operator: Operator | None, spec: ActionSpec) -> None:
        if operator is None:
            raise ActionError("E_OPERATOR_REQUIRED", f"复核「{spec.title}」需要有效席位（X-Aegis-Operator）")
        needed = self.minimum_role(spec)
        if not role_at_least(operator.role, needed):
            raise ActionError(
                "E_ACTION_REVIEW_FORBIDDEN",
                f"复核「{spec.title}」需要 {needed.value} 及以上角色，当前是 {operator.role.value}",
            )


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    """一次投票的结果。`ready=True` 表示第二票已把台账迁到 `executing`，可以接着执行动作。"""

    ready: bool
    record: dict[str, Any]


class DualReview:
    """状态机：proposed →(第一票 1/2) proposed →(第二票 2/2) executing。

    状态迁移本身在台账里（SQL 的 `UPDATE ... RETURNING` 或内存台账的同一判据），
    本类只负责校验投票资格、写出票面、把"没抢到"翻成一句可读的冲突原因。
    """

    def __init__(self, ledger: ReviewLedger, policy: ReviewPolicy) -> None:
        self._ledger = ledger
        self._policy = policy

    @property
    def enabled(self) -> bool:
        return self._policy.enabled

    async def vote(self, action_id: str, operator: Operator | None, spec: ActionSpec) -> ReviewOutcome:
        self._policy.authorize(operator, spec)
        assert operator is not None  # authorize 已挡住 None
        vote = {
            "operator_id": operator.operator_id,
            "role": operator.role.value,
            "at": datetime.now(UTC).isoformat(),
        }
        moved = await self._ledger.review_action(action_id, vote)
        if moved is None:
            current = await self._ledger.get_action(action_id)
            votes = [] if current is None else list(current.get("reviewers") or [])
            if any(str(item.get("operator_id")) == operator.operator_id for item in votes):
                raise ActionError(
                    "E_ACTION_DUPLICATE_REVIEW",
                    f"席位 {operator.operator_id} 已经投过复核票；第二票必须来自另一名席位",
                )
            raise ActionError("E_ACTION_CONFLICT", "动作已被处置或已过期，本次复核没有生效")
        record, ready = moved
        return ReviewOutcome(ready=ready, record=record)
