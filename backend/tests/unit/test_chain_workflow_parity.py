"""两条编排路径的 parity harness（D7 §4.6 第 3 步：全绿之后 chain 才允许退役）。

对的是**可观测物**，不是实现细节：同一条容器、同一个 `HazardStages`、同一套降级轨，
固定顺序 `container.chain.process()` 与 DAG `container.workflow_chain.process()` 各跑一次，
逐项要求相等——段的名字/mode/ok/note、定级、任务单元形状、预警内容、降级与故障清单、
D10 凭证落在哪几段，以及 `stage_*_ms` 的样本计数。

随机 id 与墙钟延迟刻意不比：两条独立链路本就该有不同的 id 与耗时；
而"每段是否各留一条样本"才是退役之后指标口径会不会变的真问题。
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from aegis.agents.mock import start_mock_agents
from aegis.domain.messages import new_event_id, new_trace_id
from aegis.pipeline.board import ChainBoard
from aegis.pipeline.result import STAGE_ORDER, ChainResult
from aegis.pipeline.workflow_chain import STANDARD_CHAIN_TEMPLATE, WorkflowChain
from conftest import REGION, readings_rain_burst

# 三项读数都压在阈值之下：感知无命中，用于构造"停在一段 + 后四段 skipped"这条早退路径
LOW_READINGS = readings_rain_burst(rain_10min=0.1, rain_cumulative_24h=0.2, debris_level=0.0)

#: 每条链路自己的 id：两条独立链路本来就不该相等，比之前一律归一。
_ID_PATTERN = re.compile(r"(wrn_|stu_|evt_|trc_|wfi_)[0-9a-f]{6,}")


def normalize_ids(text: str) -> str:
    return _ID_PATTERN.sub(lambda m: f"<{m.group(1).rstrip(chr(95))}>", text)


def observable(result: ChainResult) -> dict[str, Any]:
    """把 ChainResult 投影成"该相等的部分"。"""
    verdict = result.verdict
    warning = result.warning
    return {
        "ok": result.ok,
        "acted": result.acted,
        "stages": [(s.name, s.mode, s.ok, normalize_ids(s.note)) for s in result.stages],
        "stage_models": sorted(result.stage_models),
        "hits": sorted((h.rule_id, h.hazard_type, round(h.score, 6)) for h in result.hits),
        "risk": (
            None
            if verdict is None
            else (
                int(verdict.risk_level),
                verdict.hazard_type.value,
                verdict.region_code,
                round(verdict.confidence, 6),
                verdict.assessed_by,
            )
        ),
        "task_units": sorted((u.task_type.value, u.hazard_type, int(u.priority), u.created_by) for u in result.task_units),
        "warning": (
            None
            if warning is None
            else (
                int(warning.risk_level),
                warning.hazard_type,
                list(warning.region_codes),
                list(warning.audiences),
                list(warning.channels),
                warning.title_zh,
                warning.body_zh,
                len(warning.deliveries),
            )
        ),
        "degradations": list(result.degradations),
        "errors": list(result.errors),
        "reference_cases": list(result.reference_cases),
        "context_docs": list(result.context_docs),
    }


def stage_sample_counts(container: Any) -> dict[str, int]:
    ledger = container.tracer.ledger
    return {name: int(ledger.stats(f"stage_{name}_ms").count) for name in STAGE_ORDER}


async def _both(container: Any, readings: list[Any], **kwargs: Any) -> tuple[ChainResult, ChainResult]:
    return (
        await container.chain.process(readings, region_code=REGION, **kwargs),
        await container.workflow_chain.process(readings, region_code=REGION, **kwargs),
    )


class TestParity:
    async def test_本地降级轨两路径逐字段相等(self, container: Any) -> None:
        """无智能体在册：两条路径都落到同一套规则轨，且落法一模一样。"""
        fixed, dag = await _both(container, readings_rain_burst())
        assert fixed.acted and fixed.warning is not None, "对照组：这条输入必须真出预警，否则两个空对象也能相等"
        assert observable(dag) == observable(fixed)

    async def test_五段时延样本两路径各加一次(self, container: Any) -> None:
        """退役最怕指标悄悄少一条腿：两条路径各自都得把五段样本各留一条。"""
        before = stage_sample_counts(container)
        await container.chain.process(readings_rain_burst(), region_code=REGION)
        after_fixed = stage_sample_counts(container)
        await container.workflow_chain.process(readings_rain_burst(), region_code=REGION)
        after_dag = stage_sample_counts(container)
        assert {name: after_fixed[name] - before[name] for name in STAGE_ORDER} == dict.fromkeys(STAGE_ORDER, 1)
        assert {name: after_dag[name] - after_fixed[name] for name in STAGE_ORDER} == dict.fromkeys(STAGE_ORDER, 1)

    async def test_无触发命中时两边都是五条且后四条skipped(self, container: Any) -> None:
        fixed, dag = await _both(container, LOW_READINGS)
        assert not fixed.acted, "对照组：这条输入确实不该出预警，否则 skipped 那半边没被构造出来"
        assert [(s.name, s.mode) for s in fixed.stages] == [(name, "skipped" if i else "local") for i, name in enumerate(STAGE_ORDER)]
        assert observable(dag) == observable(fixed)

    async def test_有替身智能体时两路径都采纳其结论(self, container: Any) -> None:
        """DAG 不是"只能跑降级轨"的摆设：智能体在册时它同样要走到 mode=agent。"""
        agents = await start_mock_agents(container.transport, latency_ms=2.0, heartbeat_interval=0.05)
        try:
            await container.transport.idle()
            fixed, dag = await _both(container, readings_rain_burst())
        finally:
            for agent in agents:
                await agent.stop()
        assert fixed.acted and dag.acted
        assert any(s.mode == "agent" for s in dag.stages), [s.as_dict() for s in dag.stages]
        assert observable(dag) == observable(fixed)

    async def test_盘面在起图失败时也不漏(self, container: Any) -> None:
        """`finally` 收盘是这条路径唯一的内存安全网：起不来也必须收。"""
        broken = WorkflowChain(
            container.chain.stages,
            container.workflow,
            stations=container.workflow_chain.stations,
            template_name="没有这张图",
        )
        with pytest.raises(Exception, match="未找到工作流模板"):
            await broken.process(readings_rain_burst(), region_code=REGION)
        assert container.workflow_chain.stations.boards.snapshot() == {"open_boards": 0, "traces": []}


class TestDagPathIsReal:
    async def test_标准五段模板确实注册且节点是chain_stage(self, container: Any) -> None:
        """防"看起来等价其实根本没走工作流"：这条图必须真在引擎里，节点类型真是 chain_stage。"""
        definition = container.workflow.latest_definition(STANDARD_CHAIN_TEMPLATE)
        assert definition is not None, "标准五段模板没注册上，parity 就是自说自话"
        assert [node.config["stage"] for node in definition.nodes] == list(STAGE_ORDER)
        assert {node.type for node in definition.nodes} == {"chain_stage"}
        assert next((edge.source, edge.target, edge.condition) for edge in definition.edges) == ("perceive", "assess", "hit")

    async def test_节点超时预算跟LLM段事务同源(self, container: Any) -> None:
        """节点超时小于段预算时，链路还在等智能体、节点先被判死——两处必须同一个数。"""
        definition = container.workflow.latest_definition(STANDARD_CHAIN_TEMPLATE)
        assert definition is not None
        expected = int(container.settings.sla_agent_txn_ms) + 5_000
        assert {node.timeout_ms for node in definition.nodes} == {expected}

    async def test_实例真的跑完五段(self, container: Any) -> None:
        """引擎侧直接起一次图：五个节点都得 succeeded，且是段在跑（不是节点空转）。"""
        stations = container.workflow_chain.stations
        trace = new_trace_id()
        board = stations.open(
            ChainBoard(
                result=ChainResult(trace_id=trace, event_id=new_event_id()),
                trace=trace,
                region=REGION,
                readings=readings_rain_burst(),
            )
        )
        try:
            detail = await container.workflow.start_from_name(
                STANDARD_CHAIN_TEMPLATE, trace_id=board.trace, payload={"board_trace": board.trace}
            )
        finally:
            stations.close(board.trace)
        failures = [(node["node_id"], node.get("error"), node.get("notes")) for node in detail["nodes"] if node["state"] != "succeeded"]
        assert not failures, failures
        assert detail["status"] == "succeeded", detail

    def test_段名清单与工作流节点表不许漂移(self) -> None:
        """`STAGE_ORDER` 是指标样本名的真源，节点侧那份是复制品：不一致就有人对着两套名字量测。"""
        from aegis.workflow.nodes import _CHAIN_STAGES

        assert _CHAIN_STAGES == STAGE_ORDER

    async def test_盘面找不到时节点错误里留着原因(self, container: Any) -> None:
        """装配顺序错了要一眼看得懂：节点上只写"节点执行异常: KeyError"等于让值班员自己猜哪步没开盘。"""
        detail = await container.workflow.start_from_name(
            STANDARD_CHAIN_TEMPLATE, trace_id=new_trace_id(), payload={"board_trace": "trc_" + "0" * 16}
        )
        assert detail["status"] == "failed", detail
        perceive = next(node for node in detail["nodes"] if node["node_id"] == "perceive")
        assert perceive["state"] == "failed"
        assert "没有开过盘" in str(perceive.get("error")), perceive
