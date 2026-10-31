"""链路编排的形状契约（D7 §4.6 第 4 步：chain 退役之后，护栏从"两路径相等"换成"对基线相等"）。

退役前这里是 parity harness：同一容器、同一个 `HazardStages`，固定顺序与 DAG 各跑一次逐项对齐。
第 4 步把固定顺序那份实现删掉之后，"和另一条路径一样"这句话就没有对照物了——但**它当初量出来的
那些值仍然是契约**：段序、mode、每段的 note、定级、任务单元、预警正文、降级与故障清单、
D10 凭证落在哪几段、五段时延样本各加几条。所以这一支把它们写成字面基线。

基线怎么来的（不是手挑的"看着顺眼"的值）：2026-10-16 用 `.tmp-verify/dump_chain_baseline.py`
在两条路径都还在的时候各跑一遍、确认投影相等之后，取固定顺序那份的输出逐字段抄进来。
从此任何一个值变了，都是"对外口径变了"，要在这里改并说清理由——这正是退役前 parity 用例在做的事。

刻意不比的两样：随机 id 与墙钟延迟（两条独立链路本就该不同），所以 id 先归一。
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from aegis.agents.mock import start_mock_agents
from aegis.domain.chain import STAGE_ORDER, ChainResult, StageResult
from aegis.domain.messages import new_event_id, new_trace_id
from aegis.pipeline.board import ChainBoard
from aegis.pipeline.workflow_chain import STANDARD_CHAIN_TEMPLATE, ChainStations, WorkflowChain
from aegis.workflow.engine import WorkflowEngine
from aegis.workflow.nodes import WorkflowServices
from conftest import REGION, readings_rain_burst

# 三项读数都压在阈值之下：感知无命中，用于构造"停在一段 + 后四段 skipped"这条早退路径
LOW_READINGS = readings_rain_burst(rain_10min=0.1, rain_cumulative_24h=0.2, debris_level=0.0)

#: 每条链路自己的 id：归一之后才可比。
_ID_PATTERN = re.compile(r"(wrn_|stu_|evt_|trc_|wfi_)[0-9a-f]{6,}")

RULE_WARNING_BODY = (
    "监测区域 540121 判定为橙色泥石流风险，置信度 95%。依据：[R-DEBRIS-RAIN-1] RG-01:rain_10min=42.0>=30.0。"
    "处置建议：远离沟道与低洼河道，禁止横穿河滩，按预案线路向高处转移。"
)
AGENT_WARNING_BODY = (
    "监测区域 540121 判定为橙色泥石流风险，置信度 82%。依据：mock 研判：1 条触发命中。"
    "处置建议：远离沟道与低洼河道，禁止横穿河滩，按预案线路向高处转移。"
)

#: 退役前两条路径共同的投影（固定顺序那一份的实测输出）。
BASELINE: dict[str, dict[str, Any]] = {
    "rule_track": {
        "ok": True,
        "acted": True,
        "stages": [
            ["perceive", "local", True, "命中 1 条（智能体 0 条）"],
            ["assess", "local", True, "等级 2"],
            ["plan", "local", True, "生成 5 个任务单元"],
            ["execute", "local", True, "<wrn>"],
            ["feedback", "local", True, "触达回执已归档"],
        ],
        "stage_models": [],
        "hits": [["R-DEBRIS-RAIN-1", "debris_flow", 1.0]],
        "risk": [2, "debris_flow", "540121", 0.95, "platform.risk_engine"],
        "task_units": [
            ["evacuate", "debris_flow", 1, "platform.task_parser"],
            ["monitor", "debris_flow", 2, "platform.task_parser"],
            ["report", "debris_flow", 4, "platform.task_parser"],
            ["verify", "debris_flow", 2, "platform.task_parser"],
            ["warn", "debris_flow", 1, "platform.task_parser"],
        ],
        "warning": [
            2,
            "debris_flow",
            ["540121"],
            ["residents", "schools", "rescue_teams", "commanders"],
            ["sms", "broadcast", "wechat"],
            "西藏泥石流预警（橙色）",
            RULE_WARNING_BODY,
            3,
        ],
        "degradations": [],
        "errors": [],
        "reference_cases": ["case_df_gully_evacuate", "case_df_night_rain_patrol", "case_df_road_closure_dispatch"],
        "context_docs": [],
    },
    "no_hit": {
        "ok": True,
        "acted": False,
        "stages": [
            ["perceive", "local", True, "命中 0 条（智能体 0 条）"],
            ["assess", "skipped", True, "无触发条件命中"],
            ["plan", "skipped", True, "无触发条件命中"],
            ["execute", "skipped", True, "无触发条件命中"],
            ["feedback", "skipped", True, "无触发条件命中"],
        ],
        "stage_models": [],
        "hits": [],
        "risk": None,
        "task_units": [],
        "warning": None,
        "degradations": [],
        "errors": [],
        "reference_cases": [],
        "context_docs": [],
    },
    "agent_track": {
        "ok": True,
        "acted": True,
        "stages": [
            ["perceive", "local", True, "命中 1 条（智能体 0 条）"],
            ["assess", "agent", True, "等级 2"],
            ["plan", "agent", True, "生成 5 个任务单元"],
            ["execute", "agent", True, "<wrn>"],
            ["feedback", "local", True, "触达回执已归档"],
        ],
        "stage_models": [],
        "hits": [["R-DEBRIS-RAIN-1", "debris_flow", 1.0]],
        "risk": [2, "debris_flow", "540121", 0.82, "assess.mock01"],
        "task_units": [
            ["evacuate", "debris_flow", 1, "plan.mock01"],
            ["monitor", "debris_flow", 2, "plan.mock01"],
            ["report", "debris_flow", 4, "plan.mock01"],
            ["verify", "debris_flow", 2, "plan.mock01"],
            ["warn", "debris_flow", 1, "plan.mock01"],
        ],
        "warning": [
            2,
            "debris_flow",
            ["540121"],
            ["residents", "schools", "rescue_teams", "commanders"],
            ["sms", "broadcast", "wechat"],
            "西藏泥石流预警（橙色）",
            AGENT_WARNING_BODY,
            3,
        ],
        # 这两条是替身智能体的既有形状：它自报的通道结果没有平台凭证，反馈结论也不合契约。
        # 记在基线里是因为退役前后都该一模一样——把它们"修没"才是事故。
        "degradations": [
            "执行智能体自报 3 条通道结果未采信（无 dispatcher 凭证）：触达证据只认平台交付腿回执",
            "反馈结论不合契约，按平台回执归档",
        ],
        "errors": [],
        "reference_cases": ["case_df_gully_evacuate", "case_df_night_rain_patrol", "case_df_road_closure_dispatch"],
        "context_docs": [],
    },
}


def normalize_ids(text: str) -> str:
    return _ID_PATTERN.sub(lambda m: f"<{m.group(1).rstrip(chr(95))}>", text)


def observable(result: ChainResult) -> dict[str, Any]:
    """把 ChainResult 投影成"该相等的那部分"（退役前 parity 用的同一个投影）。"""
    verdict = result.verdict
    warning = result.warning
    return _as_lists(
        {
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
    )


def _as_lists(value: Any) -> Any:
    """投影里的元组一律换成列表：基线是从 JSON 抄来的，而 JSON 没有元组。

    刻意不留"认不出就 str()"的兜底——那会把类型漂移静默成一句字符串差异，
    读的人得自己猜是哪个字段换了类型。
    """
    if isinstance(value, tuple):
        return [_as_lists(item) for item in value]
    if isinstance(value, list):
        return [_as_lists(item) for item in value]
    if isinstance(value, dict):
        return {key: _as_lists(item) for key, item in value.items()}
    return value


def stage_sample_counts(container: Any) -> dict[str, int]:
    ledger = container.tracer.ledger
    return {name: int(ledger.stats(f"stage_{name}_ms").count) for name in STAGE_ORDER}


class TestShapeContractAgainstBaseline:
    async def test_链路入口就是DAG那条(self, container: Any) -> None:
        """退役的判定点在这儿：`chain` 这个名字底下已经是工作流编排，不是 Python 写死的顺序。"""
        assert isinstance(container.chain, WorkflowChain)

    async def test_规则档产出与基线逐项相同(self, container: Any) -> None:
        result = await container.chain.process(readings_rain_burst(), region_code=REGION)
        assert observable(result) == BASELINE["rule_track"], "对外口径变了：改基线要写清理由"

    async def test_无触发命中时停在一段且后四条记skipped(self, container: Any) -> None:
        result = await container.chain.process(LOW_READINGS, region_code=REGION)
        assert observable(result) == BASELINE["no_hit"]
        assert [(s.name, s.mode) for s in result.stages] == [(name, "skipped" if i else "local") for i, name in enumerate(STAGE_ORDER)]

    async def test_有替身智能体时采纳其结论并留下同样的降级(self, container: Any) -> None:
        agents = await start_mock_agents(container.transport, latency_ms=2.0, heartbeat_interval=0.05)
        try:
            await container.transport.idle()
            result = await container.chain.process(readings_rain_burst(), region_code=REGION)
        finally:
            for agent in agents:
                await agent.stop()
        assert any(s.mode == "agent" for s in result.stages), [s.as_dict() for s in result.stages]
        assert observable(result) == BASELINE["agent_track"]

    async def test_每次链路把五段时延样本各加一条(self, container: Any) -> None:
        """退役最怕指标悄悄少一条腿：段名就是样本名的一部分，这里把它钉成 +1。"""
        before = stage_sample_counts(container)
        await container.chain.process(readings_rain_burst(), region_code=REGION)
        after = stage_sample_counts(container)
        assert {name: after[name] - before[name] for name in STAGE_ORDER} == dict.fromkeys(STAGE_ORDER, 1)

    async def test_多区域并发时样本按区域数增长且投影与基线同形(self, container: Any) -> None:
        before = stage_sample_counts(container)
        results = await container.chain.process_many({REGION: readings_rain_burst(), "540122": readings_rain_burst(rain_10min=60.0)})
        after = stage_sample_counts(container)
        assert len(results) == 2
        assert observable(results[0]) == BASELINE["rule_track"]
        assert {name: after[name] - before[name] for name in STAGE_ORDER} == {
            "perceive": 2,
            "assess": 1,
            "plan": 1,
            "execute": 1,
            "feedback": 1,
        }, "第二个区域无命中：只该多一条感知样本，后四段是 skipped 不计时"

    async def test_盘面在起图失败时也不漏(self, container: Any) -> None:
        """`finally` 收盘是这条路径唯一的内存安全网：起不来也必须收。"""
        broken = WorkflowChain(
            container.chain.stages,
            container.workflow,
            stations=container.chain.stations,
            template_name="没有这张图",
        )
        with pytest.raises(Exception, match="未找到工作流模板"):
            await broken.process(readings_rain_burst(), region_code=REGION)
        assert container.chain.stations.boards.snapshot() == {"open_boards": 0, "traces": []}


class TestDagPathIsReal:
    async def test_标准五段模板确实注册且节点是chain_stage(self, container: Any) -> None:
        """防"看起来等价其实根本没走工作流"：这条图必须真在引擎里，节点类型真是 chain_stage。"""
        definition = container.workflow.latest_definition(STANDARD_CHAIN_TEMPLATE)
        assert definition is not None, "标准五段模板没注册上，这份契约就是自说自话"
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
        stations = container.chain.stations
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
        # 段留下的可读原因必须同时是节点留痕：画布上答"这一段为什么这样"靠的是 `notes`，
        # 只把 note 记进链路台账的话，实例视图上就只剩五个绿勾。
        by_id = {node["node_id"]: node for node in detail["nodes"]}
        assert any("命中 1 条" in note for note in by_id["perceive"]["notes"]), by_id["perceive"]["notes"]
        assert any("触达回执已归档" in note for note in by_id["feedback"]["notes"]), by_id["feedback"]["notes"]

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


class TestInstanceTerminalState:
    """链路产物必须带上承载它的实例终态。

    段账只记"跑过的段"：实例在段之间失败、被中止或挂在人工节点时，已跑的段仍然各自 ok，
    只看 `stages` 就会把一条没走完的链路报成 `ok=True`（P0-3）。
    """

    @staticmethod
    async def _chain(container: Any, *, name: str, nodes: list[dict], edges: list[dict]) -> tuple[WorkflowChain, WorkflowEngine]:
        stations = ChainStations(container.chain.stages)
        engine = WorkflowEngine(
            services=WorkflowServices(run_chain_stage=stations, http_call=_failing_http_call),
            tracer=container.tracer,
            settings=container.settings,
        )
        await engine.create_definition(name=name, description="", nodes=nodes, edges=edges)
        return WorkflowChain(container.chain.stages, engine, stations=stations, template_name=name), engine

    def test_离线构造的结果不因缺省实例字段被改判(self) -> None:
        """没有实例可看的结果（单测/离线构造）沿用旧口径，且不输出空的 instance_status。"""
        result = ChainResult(trace_id="trc_" + "0" * 16, event_id="evt_" + "0" * 12)
        result.stages.append(StageResult("perceive", "local", True, 1.0))
        assert result.ok is True
        assert "instance_status" not in result.as_dict()

    async def test_实例失败时链路不得报成功(self, container: Any) -> None:
        chain, _ = await self._chain(
            container,
            name="失败链路",
            nodes=[
                {"node_id": "perceive", "type": "chain_stage", "config": {"stage": "perceive"}},
                {
                    "node_id": "boom",
                    "type": "api_call",
                    "config": {"method": "GET", "url": "http://example.test/x"},
                    "on_failure": "abort",
                    "retry": {"max_attempts": 0},
                },
            ],
            edges=[{"source": "perceive", "target": "boom"}],
        )
        result = await chain.process(readings_rain_burst(), region_code=REGION)

        assert result.instance_status == "failed"
        assert result.verdict is None, "第二段失败后不该继续跑研判"
        assert result.ok is False, "实例没跑完，链路却被报成成功"
        assert any("未完成" in error for error in result.errors), result.errors
        assert result.as_dict()["ok"] is False and result.as_dict()["instance_status"] == "failed"

    async def test_等待人工的实例不算成功(self, container: Any) -> None:
        """等待是"未完成"而不是"完成"：段账全绿也必须让调用方看见实例还挂着。"""
        chain, _ = await self._chain(
            container,
            name="等待链路",
            nodes=[
                {"node_id": "perceive", "type": "chain_stage", "config": {"stage": "perceive"}},
                {"node_id": "sign", "type": "human_review", "config": {}},
            ],
            edges=[{"source": "perceive", "target": "sign"}],
        )
        result = await chain.process(readings_rain_burst(), region_code=REGION)

        assert result.instance_status == "waiting"
        assert all(stage.ok for stage in result.stages), "已跑的段本身是成功的"
        assert result.ok is False, "挂在人工节点上的链路不是走完了"
        assert result.errors == [], "等待人工不是错误，原因写在实例状态里而不是 errors"


async def _failing_http_call(_method: str, _url: str, _body: object = None) -> dict[str, Any]:
    raise RuntimeError("外呼失败")
