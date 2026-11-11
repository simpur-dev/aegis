"""Prompt 注册表（§4.1.6 / D8）：按 task 键存放版本化提示词，产出结论可复现的凭证。

**唯一真源**：平台内环的 6 条与灾种模块的 16 条**同在一个注册表**里（文本分别在
`prompt_texts_platform.py` / `prompt_texts_hazard.py`，装配在 `builtin()`）。
此前是两份：平台侧版本化、灾种侧是模块级 Python 常量（无版本号），于是灾种结论的
`prompt_version` 恒为 None——"结论可复现"只对一半的智能体成立。

版本语义：每条 (task) 记 `{version, system, user}`，`user` 是 `str.format` 模板；
prompt 版本号随结论写进 `model_info`（D10），是评测回归与论文"结论可复现"的前提（D8）。
未登记的 task，`version_for_task` 如实回 None，绝不编一个版本号。

**为什么在 L3 的 `agents/core/`（而不是 L4 的 `services/llm/`）**：灾种包的边界门禁是
白名单制，共享件必须落在两侧都能依赖的契约面里；否则灾种 import 它就是 L3→L4 倒挂
（ADR-0017）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    task: str
    version: str
    system: str
    user: str

    def build(self, **vars: object) -> list[dict[str, str]]:
        try:
            rendered = self.user.format(**vars)
        except KeyError as exc:
            raise ValueError(f"prompt {self.task} 缺少模板变量 {exc.args[0]!r}") from exc
        return [{"role": "system", "content": self.system}, {"role": "user", "content": rendered}]


#: 已迁入版本化真源的智能体提示词（单一真源）。迁入一个智能体才登记一个；
#: 未迁入的（如 plan）不登记，其 model_info.prompt_version 如实为 None，避免假可复现。
class PromptRegistry:
    def __init__(self, templates: list[PromptTemplate] | None = None) -> None:
        self._by_task: dict[str, PromptTemplate] = {}
        for template in templates or []:
            self._by_task[template.task] = template

    @classmethod
    def builtin(cls) -> PromptRegistry:
        """内置全部已迁入版本化真源的提示词（平台 6 条 + 灾种 16 条）。

        只登记【已迁入】的 task——未迁入的智能体 `version_for_task` 仍返回 None（诚实：没有
        版本化 prompt 就说没有），避免"版本号来自注册表、实际正文却内联在别处"的假可复现。
        操作员可用 `from_dir` 覆盖（部署时把 prompts/ 打进镜像）。
        """
        from aegis.agents.core.prompt_texts_hazard import HAZARD_TEMPLATES
        from aegis.agents.core.prompt_texts_platform import PLATFORM_TEMPLATES

        return cls([*PLATFORM_TEMPLATES, *HAZARD_TEMPLATES])

    @classmethod
    def from_dir(cls, path: str | Path) -> PromptRegistry:
        """从目录装载版本化提示词：每个 `<task>.json` 是一条 `{version, system, user}`。

        装载是启动期一次性事实：坏 JSON / 缺字段直接抛，让装配点显式降级，
        而不是运行到某条智能体才发现 prompt 拼不出来。
        """
        root = Path(path)
        templates: list[PromptTemplate] = []
        if not root.is_dir():
            raise FileNotFoundError(f"prompt 目录不存在: {root}")
        for file in sorted(root.glob("*.json")):
            payload = json.loads(file.read_text(encoding="utf-8"))
            templates.append(
                PromptTemplate(
                    task=file.stem,
                    version=str(payload["version"]),
                    system=str(payload["system"]),
                    user=str(payload["user"]),
                )
            )
        return cls(templates)

    def register(self, template: PromptTemplate) -> None:
        self._by_task[template.task] = template

    def get(self, task: str) -> PromptTemplate | None:
        return self._by_task.get(task)

    def version_for_task(self, task: str) -> str | None:
        """未登记即 None：诚实标注"这条没有版本化 prompt"，不臆造版本号。"""
        found = self._by_task.get(task)
        return found.version if found is not None else None

    def build(self, task: str, /, **vars: object) -> tuple[list[dict[str, str]], str | None]:
        """按 task 装配消息，返回 (messages, prompt_version)；未登记 task 直接 KeyError。

        `task` 是**仅位置参数**：提示词正文里本来就有名为 `task` 的变量（"原始任务：{task}"），
        位置参数才不至于与它撞名。
        """
        found = self._by_task.get(task)
        if found is None:
            raise KeyError(f"未登记的 prompt task: {task}")
        return found.build(**vars), found.version

    def tasks(self) -> list[str]:
        return sorted(self._by_task)


def build_prompt(prompt_task: str, /, **vars: object) -> tuple[list[dict[str, str]], str | None]:
    """按 task 装配消息，返回 (messages, prompt_version)。

    走默认注册表（模块级共享）：调用方不必持有 registry 实例——灾种模块的图节点就是
    这种，它们按 task 键装配消息。

    `prompt_version` 随消息一起返回：调用方要把它写进台账与 `model_info`（D10），
    否则"版本号描述的不是实际发送的正文"。
    """
    return default_registry().build(prompt_task, **vars)


@lru_cache(maxsize=1)
def default_registry() -> PromptRegistry:
    """模块级共享的注册表（内置模板）。

    给**不持有 registry** 的调用方用——灾种模块的图节点就是这种：它们按 task 键装配
    消息，拿不到网关实例，也不该为了拼一条提示词去 new 一个注册表。

    `PromptRegistry.builtin()` 仍返回**新实例**：就地 `register()` 不该污染全局
    （`tests/unit/test_llm_gateway_v2.py` 依赖这一点）。
    """
    return PromptRegistry.builtin()
