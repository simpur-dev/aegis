"""推演这一条腿的装配翻译：案例命中 → 节点要的键，配置 + 画布参数 → 沙盒调用口。

（P4 纯搬运：`AssembledScenarios` 与 `sandbox_runner` 的函数体逐字未改，原先住在 `container.py`，
后者当时叫 `_sandbox_runner`——出了容器就不是谁的私有辅助了，所以去掉下划线。）
DAG 节点 `situation_simulate` 的契约不变，变的是实现从哪来——所以"怎么把平台既有能力折成
节点要的调用口"这件事属于装配层，不属于工作流引擎，也不属于沙盒本体。
"""

from __future__ import annotations

from typing import Any

from aegis.config import Settings
from aegis.knowledge.provider import KnowledgeProvider


class AssembledScenarios:
    """把知识层的案例命中折成推演节点要的键（装配点翻译；内核只认 `ScenarioProvider` 协议）。

    刻意不捕异常：召回失败要作为 `situation_simulate` 的降级事实外显（节点侧写 `degraded`），
    在这里圆成"空案例"就等于把图谱挂了说成"没有相似案例"。
    图谱侧只回节点摘要（`case is None`）时，`planning_brief()` 就只有命中证据——
    推演节点拿不到 `estimated_delay_hours` 就不输出该要素，谁也不替案例补数。
    """

    def __init__(self, *, knowledge: KnowledgeProvider, settings: Settings) -> None:
        self._knowledge = knowledge
        self._settings = settings

    async def recall_scenarios(
        self,
        *,
        hazard_type: str | None = None,
        region_code: str | None = None,
        risk_level: int | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        query = " ".join(
            part
            for part in (
                hazard_type or "",
                region_code or "",
                "" if risk_level is None else f"{int(risk_level)}级",
            )
            if part
        )
        matches = await self._knowledge.recall(
            query or "山地灾害",
            hazard_type=hazard_type,
            region_code=region_code,
            limit=int(limit),
            budget_ms=self._settings.knowledge_recall_budget_ms,
        )
        return [brief for brief in (match.planning_brief() for match in matches) if brief.get("case_id")]


def sandbox_runner(settings: Settings, llm_gateway: object | None) -> Any:
    """把推演沙盒接成 DAG 节点 `situation_simulate` 要的调用口（§4.4：节点契约不变，能力换实现）。

    画布/演练给了参数就用给的，没给用配置默认。**每个参数都夹进区间**：
    节点输入来自画布上人手填的 config，`scenarios=1e9` 不该变成一次跑不完的循环。
    """
    from aegis.services.simulation import SimConfig, make_sandbox

    defaults = SimConfig()

    async def _run(payload: dict[str, Any]) -> dict[str, Any]:
        def _clamped_int(key: str, *, low: int, high: int) -> int:
            raw = payload.get(key)
            value = int(getattr(defaults, key) if raw is None else raw)
            return max(low, min(high, value))

        raw_seed = payload.get("seed")
        cfg = SimConfig(
            scenarios=_clamped_int("scenarios", low=1, high=64),
            rounds=_clamped_int("rounds", low=1, high=24),
            rain_base_mm=float(payload.get("rain_base_mm", defaults.rain_base_mm)),
            rain_spread_mm=float(payload.get("rain_spread_mm", defaults.rain_spread_mm)),
            playbook=str(payload.get("playbook") or defaults.playbook),
            token_budget=_clamped_int("token_budget", low=1_000, high=defaults.token_budget),
            seed=int(defaults.seed if raw_seed is None else raw_seed),
        )
        # 括号是必需的：`await x.run(cfg).as_dict()` 会被解析成 `await (x.run(cfg).as_dict())`。
        return (await make_sandbox(settings, llm_gateway=llm_gateway).run(cfg)).as_dict()

    return _run
