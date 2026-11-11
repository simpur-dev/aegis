"""提示词版本门禁：版本号必须描述**实际发送的正文**。

D10 承诺"结论可复现"，前提是 `model_info.prompt_version` 真的对应那一次的提示词正文。
这条门禁把两件事钉住：

1. **全部灾种模板都登记了版本号**，且与平台模板同处一个注册表；
2. **正文指纹**：每条模板的 system+user 文本有稳定哈希。正文改了而版本号没升，
   这条就红——"悄悄改提示词"这件事必须留下版本号变更的痕迹。

指纹写死在下面的常量里。改正文时**必须**同时改版本号并更新指纹——那正是 D10 要的
可追溯性，不是额外的麻烦。
"""

from __future__ import annotations

import hashlib

from aegis.agents.core.prompts import PromptRegistry

#: 灾种模板的正文指纹（system + user 的 sha256 前 16 位）。
#: 改正文必须升版本号并同步更新这里。
_HAZARD_FINGERPRINTS: dict[str, str] = {
    "hazard.data_plan": "415ff741f02e8f54",
    "hazard.data_plan.retry": "6840684ea71a203d",
    "hazard.feedback_analyze": "14a33efee39e581d",
    "hazard.feedback_reflection": "d55508e55ad0a0fe",
    "hazard.feedback_suggestion": "be9845586280da95",
    "hazard.manager_report": "0899e7ae3b64c6b2",
    "hazard.results_analysis": "e88e03d9f86c3d76",
    "hazard.results_analysis.retry": "29f64bbf1ab9238b",
    "hazard.risk_explain": "2f84e909a6373972",
    "hazard.risk_explain.retry": "8f197e668fb76fd2",
    "hazard.risk_validate": "789027d22793e960",
    "hazard.scenario_design": "7136c0467bda2286",
    "hazard.scenario_validate": "84edafbccce11bbd",
    "hazard.warning_explain": "18ab6f0addb2ea57",
    "hazard.warning_explain.retry": "fddddb90642692fd",
}


def _fingerprint(system: str, user: str) -> str:
    digest = hashlib.sha256((system + "\x00" + user).encode("utf-8")).hexdigest()
    return digest[:16]


def test_灾种模板全部登记且版本号非空() -> None:
    registry = PromptRegistry.builtin()
    hazard_tasks = [task for task in registry.tasks() if task.startswith("hazard.")]
    assert len(hazard_tasks) == 15, f"灾种模板应为 15 条，实际 {len(hazard_tasks)}: {hazard_tasks}"
    for task in hazard_tasks:
        version = registry.version_for_task(task)
        assert version, f"{task} 没有版本号——D10 的 prompt_version 就无处可写"
        assert version.count(".") == 2, f"{task} 的版本号不是语义化三段式: {version!r}"


def test_每个重试变体都有对应的基础模板() -> None:
    """`<task>.retry` 必须有一条 `<task>` 与之配对（重试是基础正文 + 追加段）。"""
    registry = PromptRegistry.builtin()
    tasks = set(registry.tasks())
    retries = {task for task in tasks if task.endswith(".retry")}
    assert retries, "至少应有一条重试变体（5 个 builder 有重试分支）"
    for retry in retries:
        base = retry.removesuffix(".retry")
        assert base in tasks, f"{retry} 没有对应的基础模板 {base}"
        assert (registry.version_for_task(retry) or "") == (registry.version_for_task(base) or ""), (
            f"{retry} 与 {base} 的版本号应一致（重试变体是同一份正文加一段）"
        )


def test_正文指纹与版本号同步() -> None:
    """正文改了而版本号没升 → 红。

    首次运行会在断言消息里给出全部指纹，把值回填到 `_HAZARD_FINGERPRINTS` 即可
    （空字典表示"还没登记"，跳过比对而不是假装通过）。
    """
    registry = PromptRegistry.builtin()
    current: dict[str, str] = {}
    for task in registry.tasks():
        if not task.startswith("hazard."):
            continue
        template = registry.get(task)
        assert template is not None
        current[task] = _fingerprint(template.system, template.user)

    if not _HAZARD_FINGERPRINTS:
        # 还没登记：把当前指纹打进断言消息，方便一次性回填。
        raise AssertionError(
            "灾种模板指纹尚未登记。把下面这份字典填进 _HAZARD_FINGERPRINTS：\n"
            + "\n".join(f'    "{task}": "{value}",' for task, value in sorted(current.items()))
        )

    assert set(current) == set(_HAZARD_FINGERPRINTS), (
        f"模板集合变了：新增 {sorted(set(current) - set(_HAZARD_FINGERPRINTS))}，删除 {sorted(set(_HAZARD_FINGERPRINTS) - set(current))}"
    )
    changed = [task for task in sorted(current) if current[task] != _HAZARD_FINGERPRINTS[task]]
    assert changed == [], f"这些模板的正文改了而版本号没升（或指纹没同步）: {changed}"
