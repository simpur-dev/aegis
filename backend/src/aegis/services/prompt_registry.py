"""Prompt 注册表（§4.1.6 / D8）：按 task 键存放版本化提示词，产出结论可复现的凭证。

为什么不在这里内置提示词：平台既有用途（翻译/润色/语义解析/助手意图）的提示词各在调用方
内联一份。把它们再抄进注册表就是"第二真源两张皮"——改了内联、忘了这里，注册表版本号就成了
谎话。所以注册表的真源是 `prompts/` 目录里的版本化文件（MA 阶段随智能体逐个登记），
`builtin()` 刻意返回空表；谁没登记，`version_for_task` 就如实回 None，绝不编一个版本号。

版本语义：每条 (task) 记 `{version, system, user}`，`user` 是 `str.format` 模板；
prompt 版本号随结论写进 `model_info`（D10），是评测回归与论文"结论可复现"的前提（D8）。
放在内核 services 环（不放 knowledge 可选腿）：网关 v2 要用它，而铁律 3 禁止内核 import 可选腿。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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


class PromptRegistry:
    def __init__(self, templates: list[PromptTemplate] | None = None) -> None:
        self._by_task: dict[str, PromptTemplate] = {}
        for template in templates or []:
            self._by_task[template.task] = template

    @classmethod
    def builtin(cls) -> PromptRegistry:
        """空注册表：真源在 `prompts/` 目录，内置不复制内联提示词（避免两张皮）。"""
        return cls([])

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

    def build(self, task: str, **vars: object) -> tuple[list[dict[str, str]], str | None]:
        """按 task 装配消息，返回 (messages, prompt_version)；未登记 task 直接 KeyError。"""
        found = self._by_task.get(task)
        if found is None:
            raise KeyError(f"未登记的 prompt task: {task}")
        return found.build(**vars), found.version

    def tasks(self) -> list[str]:
        return sorted(self._by_task)
