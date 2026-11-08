"""动作注册表：全平台唯一的动作目录。

三条硬口径：
1. **只登记当期待实现的动作**。下一期的动作在需求里、不在代码里——空壳登记等于死代码，
   而且会让 `capabilities` 对用户承诺一个点了就报错的按钮。
2. **启动即自洽校验**。`assert_consistent()` 在容器装配时跑，失败就是启动失败：一个坏注册表
   带着线上跑，错误会推迟到某个人真的点了那个动作才暴露。
3. **权限判定只在这里读 spec**。路由层不再写 if/elif 判角色，避免两套规则漂移。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel

from aegis.domain.operator import OperatorRole, role_at_least
from aegis.services.assistant.base import Action, ActionDomain, ActionError

_NAME_RE = re.compile(r"^[a-z_]+\.[a-z_]+$")


class ActionRegistry:
    def __init__(self, actions: Iterable[Action], *, domains: Iterable[ActionDomain]) -> None:
        self._actions = {action.spec.name: action for action in actions}
        self._domains = frozenset(domains)

    # ---------- 查询面 ----------

    @property
    def actions(self) -> tuple[Action, ...]:
        return tuple(self._actions.values())

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._actions))

    def get(self, name: str) -> Action | None:
        return self._actions.get(name)

    def require(self, name: str) -> Action:
        action = self._actions.get(name)
        if action is None:
            raise ActionError("E_ACTION_NOT_FOUND", f"动作未注册：{name}")
        return action

    def capabilities(self, role: OperatorRole | None) -> list[dict[str, Any]]:
        """按角色给出动作清单。角色不够的动作照样列出（界面要能解释"缺什么"），只是标记不可用。"""
        rows: list[dict[str, Any]] = []
        for action in self._actions.values():
            spec = action.spec
            allowed = role is not None and role_at_least(role, spec.required_role)
            rows.append(
                {
                    "action": spec.name,
                    "title": spec.title,
                    "domain": spec.domain.value,
                    "required_role": spec.required_role.value,
                    "needs_confirm": spec.needs_confirm,
                    "reversible": spec.reversible,
                    "dual_review": spec.dual_review,
                    "example": spec.example,
                    "allowed": allowed,
                    "denied_reason": None if allowed else f"需要 {spec.required_role.value} 及以上角色",
                }
            )
        return rows

    # ---------- 启动自洽校验（失败即启动失败） ----------

    def assert_consistent(self) -> None:
        if not self._actions:
            raise AssertionError("动作注册表为空：助手将没有任何可用动作")

        for name, action in self._actions.items():
            spec = action.spec
            if spec.name != name:
                raise AssertionError(f"动作键与 spec.name 不一致：{name} != {spec.name}")
            if not _NAME_RE.match(name):
                raise AssertionError(f"动作名必须形如 domain.verb：{name}")
            if spec.domain not in self._domains:
                raise AssertionError(f"动作 {name} 的域 {spec.domain} 不在本期实现范围内")
            if not (isinstance(spec.params, type) and issubclass(spec.params, BaseModel)):
                raise AssertionError(f"动作 {name} 的 params 必须是 BaseModel 子类")
            if spec.params.model_config.get("extra") != "forbid":
                raise AssertionError(f"动作 {name} 的 params 必须 extra='forbid'")
            if spec.needs_confirm and not (spec.reversible or spec.previewable):
                raise AssertionError(f"动作 {name} 需要确认，但既不可撤销也不生成预览：确认没有信息量")
            # 复核票只能挂在"本来就有确认闸"的动作上：读动作与直接执行类动作没有可等待的状态，
            # 标了就是配了不生效的空旋钮（assert_consistent 在装配期把它钉死）。
            if spec.dual_review and not (spec.needs_confirm and spec.previewable):
                raise AssertionError(f"动作 {name} 标了双人复核，但没有确认闸或影响预览：票无处可投")
            if spec.reversible and type(action).revoke is Action.revoke:
                raise AssertionError(f"动作 {name} 声明可撤销，但没有实现 revoke")
            if not isinstance(spec.required_role, OperatorRole):
                raise AssertionError(f"动作 {name} 的 required_role 不是合法角色")

        implemented = {action.spec.domain for action in self._actions.values()}
        missing = self._domains - implemented
        if missing:
            raise AssertionError("本期声明的域没有任何动作：" + "、".join(sorted(domain.value for domain in missing)))
