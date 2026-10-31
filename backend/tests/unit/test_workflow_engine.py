"""柔性工作流引擎测试：调度、分支、并行、失败策略、人工介入、运行中改图、时延指标。"""

from __future__ import annotations

import asyncio

import pytest

from aegis.config import Settings
from aegis.observability.tracer import Tracer
from aegis.workflow.engine import WorkflowEngine, WorkflowValidationError
from aegis.workflow.nodes import WorkflowServices
from aegis.workflow.store import WorkflowRepository


def make_engine(services: WorkflowServices | None = None, **kwargs: object) -> WorkflowEngine:
    return WorkflowEngine(
        services=services or WorkflowServices(),
        tracer=Tracer(),
        settings=Settings(env="test"),
        **kwargs,  # type: ignore[arg-type]
    )


async def make_definition(engine: WorkflowEngine, nodes: list[dict], edges: list[dict], name: str = "测试流程") -> str:
    definition = await engine.create_definition(name=name, description="", nodes=nodes, edges=edges)
    return definition.workflow_id


def node(node_id: str, type_name: str, config: dict | None = None, **overrides: object) -> dict:
    payload: dict = {"node_id": node_id, "type": type_name, "config": config or {}}
    payload.update(overrides)
    return payload


def edge(source: str, target: str, condition: str = "") -> dict:
    return {"source": source, "target": target, "condition": condition}


class TestDefinitionGuard:
    async def test_unknown_node_type_rejected(self) -> None:
        engine = make_engine()
        with pytest.raises(WorkflowValidationError, match="未注册的节点类型"):
            await make_definition(engine, [node("a", "telepathy")], [])

    async def test_missing_required_config_rejected(self) -> None:
        engine = make_engine()
        with pytest.raises(WorkflowValidationError, match="缺少必填参数"):
            await make_definition(engine, [node("a", "notify", {})], [])

    async def test_unknown_config_key_rejected(self) -> None:
        engine = make_engine()
        with pytest.raises(WorkflowValidationError, match="未知参数"):
            await make_definition(engine, [node("a", "delay", {"seconds": 0, "warp_factor": 3})], [])

    async def test_cycle_rejected_at_creation(self) -> None:
        engine = make_engine()
        with pytest.raises(WorkflowValidationError):
            await make_definition(
                engine,
                [node("a", "delay", {"seconds": 0}), node("b", "delay", {"seconds": 0})],
                [edge("a", "b"), edge("b", "a")],
            )

    async def test_at_least_ten_node_types_available(self) -> None:
        """考核指标 2：≥10 类防控任务节点。"""
        engine = make_engine()
        assert len(engine.node_types) >= 10

    async def test_revision_creates_new_version(self) -> None:
        engine = make_engine()
        workflow_id = await make_definition(engine, [node("a", "delay", {"seconds": 0})], [])
        revised = await engine.revise_definition(
            workflow_id, nodes=[node("a", "delay", {"seconds": 0}), node("b", "delay", {"seconds": 0})], edges=[]
        )
        assert revised.version == 2
        assert revised.workflow_id != workflow_id
        assert engine.repository.versions("测试流程") == [1, 2]

    async def test_revise_unknown_definition(self) -> None:
        engine = make_engine()
        with pytest.raises(WorkflowValidationError, match="不存在"):
            await engine.revise_definition("wf_" + "0" * 12, nodes=[])

    async def test_repository_capacity_guard(self) -> None:
        engine = WorkflowEngine(repository=WorkflowRepository(capacity=1), services=WorkflowServices(), settings=Settings(env="test"))
        await make_definition(engine, [node("a", "delay", {"seconds": 0})], [], name="流程一")
        with pytest.raises(RuntimeError):
            await make_definition(engine, [node("b", "delay", {"seconds": 0})], [], name="流程二")


class TestExecutionSemantics:
    async def test_linear_order_and_outputs(self) -> None:
        seen: list[str] = []

        async def query(**_kwargs: object) -> list[dict]:
            seen.append("fetch")
            return [{"metric": "rain_10min", "value": 42.0, "quality_flag": "ok"}]

        engine = make_engine(WorkflowServices(telemetry_query=query))
        workflow_id = await make_definition(
            engine,
            [
                node("fetch", "data_fetch", {"limit": 10}),
                node(
                    "judge",
                    "threshold",
                    {"upstream": "fetch", "conditions": [{"metric": "rain_10min", "op": ">=", "threshold": 30.0}]},
                ),
            ],
            [edge("fetch", "judge")],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "1" * 16, payload={})
        assert detail["status"] == "succeeded"
        assert seen == ["fetch"]
        judge = next(n for n in detail["nodes"] if n["node_id"] == "judge")
        assert judge["output"]["triggered"] is True

    async def test_branch_skips_unselected_path(self) -> None:
        async def notify(_payload: dict) -> None:
            return None

        async def query(**_kwargs: object) -> list[dict]:
            return [{"metric": "rain_10min", "value": 5.0, "quality_flag": "ok"}]

        engine = make_engine(WorkflowServices(notify=notify, telemetry_query=query))
        workflow_id = await make_definition(
            engine,
            [
                node("fetch", "data_fetch", {"limit": 5}),
                node(
                    "judge",
                    "threshold",
                    {"upstream": "fetch", "conditions": [{"metric": "rain_10min", "op": ">=", "threshold": 999.0}]},
                ),
                node("assess", "risk_assess", {"upstream": "judge"}),
                node("quiet", "notify", {"text": "未触发"}),
            ],
            [
                edge("fetch", "judge"),
                edge("judge", "assess", "triggered"),
                edge("judge", "quiet", "not_triggered"),
            ],
        )

        async def query(**_kwargs: object) -> list[dict]:
            return [{"metric": "rain_10min", "value": 5.0, "quality_flag": "ok"}]

        detail = await engine.start(workflow_id, trace_id="trc_" + "2" * 16, payload={})
        states = {n["node_id"]: n["state"] for n in detail["nodes"]}
        assert states["quiet"] == "succeeded"
        assert states["assess"] == "skipped", "未命中分支的下游应级联跳过"
        assert detail["status"] == "succeeded"

    async def test_分支名与边条件只差大小写时也要走对出口(self) -> None:
        """`EdgeDef.condition` 存下来是 `.strip().lower()` 过的（model.py 的校验器），
        而节点发出的分支名是**原样**（`branch=str(rule["then"])`、人工核签的 choice 也是人写的字）。
        真机量到：规则 then="OK"、边条件写 "OK"，实例里 gate `succeeded` 输出 branch="OK"，
        两条下游却都是 `skipped`，实例状态还写着 `succeeded` —— 通知一条没发出去，屏幕上没有任何线索。
        """

        async def notify(_payload: dict) -> None:
            return None

        async def query(**_kwargs: object) -> list[dict]:
            return [{"metric": "rain_10min", "value": 5.0, "quality_flag": "ok"}]

        engine = make_engine(WorkflowServices(notify=notify, telemetry_query=query))
        workflow_id = await make_definition(
            engine,
            [
                node("gate", "branch", {"rules": [{"when": "risk_level", "op": ">=", "value": 1, "then": "OK"}], "default": "else"}),
                node("notify_ok", "notify", {"text": "命中 OK 分支"}),
                node("notify_else", "notify", {"text": "走默认分支"}),
            ],
            [
                edge("gate", "notify_ok", "OK"),
                edge("gate", "notify_else", "else"),
            ],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "b" * 16, payload={"risk_level": 3})
        states = {n["node_id"]: n["state"] for n in detail["nodes"]}
        gate = next(n for n in detail["nodes"] if n["node_id"] == "gate")
        assert gate["output"]["branch"] == "OK", gate["output"]
        assert states["notify_ok"] == "succeeded", f"边条件存成小写后就再也匹配不上分支名 OK：{states}"
        assert states["notify_else"] == "skipped"

    async def test_分支没有出口可走时要把原因写在节点上(self) -> None:
        """大小写统一之后仍会有一种真情况：发出的分支名根本没有对应连线。
        以前这些下游只带一句"上游分支未命中，级联跳过"，看不出**是哪个分支值**没处去。"""

        async def notify(_payload: dict) -> None:
            return None

        engine = make_engine(WorkflowServices(notify=notify))
        workflow_id = await make_definition(
            engine,
            [
                node("gate", "branch", {"rules": [{"when": "risk_level", "op": ">=", "value": 1, "then": "escalate"}], "default": "else"}),
                node("notify_ok", "notify", {"text": "只连了 ok 这一条"}),
            ],
            [edge("gate", "notify_ok", "ok")],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "c" * 16, payload={"risk_level": 3})
        gate = next(n for n in detail["nodes"] if n["node_id"] == "gate")
        joined = " ".join(gate["notes"])
        assert gate["output"]["branch"] == "escalate", gate["output"]
        assert "escalate" in joined, f"节点上没写明是哪个分支值没出口：{gate['notes']}"
        assert "ok" in joined, f"也没给出连线上的可选分支名：{gate['notes']}"

    async def test_parallel_branches_run_concurrently(self) -> None:
        """并发判据必须拿"同批测出的串行参照"来比，不能写死秒数。

        旧断言 `elapsed < 0.22`：并行真值 0.12s、串行 0.24s，阈值距串行只差 20ms——
        循环里一次偶发停顿就假红（同一族毛病本批已撞过三次，全都被 `--maxfail=1` 挡在别人后面）。
        现在同批跑一份强制串行的定义当参照，只断两者之比：机器快慢一起缩放，判定不随之变。
        """

        async def slow_query(**_kwargs: object) -> list[dict]:
            await asyncio.sleep(0.12)
            return []

        engine = make_engine(WorkflowServices(telemetry_query=slow_query))

        async def run(nodes: list[dict], edges: list[dict], trace_suffix: str) -> tuple[float, dict]:
            workflow_id = await make_definition(engine, nodes, edges)
            started = asyncio.get_running_loop().time()
            detail = await engine.start(workflow_id, trace_id="trc_" + trace_suffix * 16, payload={})
            return asyncio.get_running_loop().time() - started, detail

        fork_nodes = [
            node("root", "delay", {"seconds": 0}),
            node("left", "data_fetch", {"limit": 1}),
            node("right", "data_fetch", {"limit": 1}),
            node("join", "join", {"upstream": ["left", "right"]}),
        ]
        parallel_elapsed, detail = await run(
            fork_nodes,
            [edge("root", "left"), edge("root", "right"), edge("left", "join"), edge("right", "join")],
            "3",
        )
        assert detail["status"] == "succeeded"
        join = next(n for n in detail["nodes"] if n["node_id"] == "join")
        assert set(join["output"]["merged"]) == {"left", "right"}

        # 参照：同样的两个取数节点，但右支挂在左支之后、join 只等右支 ⇒ 只能串行，两次 sleep 都得等完。
        serial_nodes = [
            node("root", "delay", {"seconds": 0}),
            node("left", "data_fetch", {"limit": 1}),
            node("right", "data_fetch", {"limit": 1}),
            node("join", "join", {"upstream": ["right"]}),
        ]
        serial_elapsed, serial_detail = await run(
            serial_nodes,
            [edge("root", "left"), edge("left", "right"), edge("right", "join")],
            "5",
        )
        assert serial_detail["status"] == "succeeded"
        assert serial_elapsed > parallel_elapsed, f"串行参照（{serial_elapsed:.2f}s）没比并行（{parallel_elapsed:.2f}s）慢，参照不成立"
        assert parallel_elapsed < serial_elapsed * 0.75, f"并行 {parallel_elapsed:.2f}s vs 串行参照 {serial_elapsed:.2f}s：两支没同时跑"

    async def test_join_waits_for_all_upstreams(self) -> None:
        async def query(**_kwargs: object) -> list[dict]:
            return []

        engine = make_engine(WorkflowServices(telemetry_query=query))
        workflow_id = await make_definition(
            engine,
            [node("root", "delay", {"seconds": 0}), node("a", "data_fetch", {"limit": 1}), node("b", "data_fetch", {"limit": 1})],
            [edge("root", "a"), edge("root", "b"), edge("a", "b")],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "4" * 16, payload={})
        states = {n["node_id"]: n["state"] for n in detail["nodes"]}
        assert states == {"root": "succeeded", "a": "succeeded", "b": "succeeded"}


class TestFailurePolicies:
    async def test_retry_then_success(self) -> None:
        calls = {"count": 0}

        async def flaky(*_args: object, **_kwargs: object) -> dict:
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("上游接口抖动")
            return {"ok": True}

        engine = make_engine(WorkflowServices(http_call=flaky))
        workflow_id = await make_definition(
            engine,
            [
                node(
                    "call",
                    "api_call",
                    {"method": "GET", "url": "http://example.internal/x"},
                    timeout_ms=2_000,
                    retry={"max_attempts": 3, "backoff_ms": 5},
                )
            ],
            [],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "5" * 16, payload={})
        assert detail["status"] == "succeeded"
        assert calls["count"] == 3
        run = detail["nodes"][0]
        assert run["attempts"] == 3
        assert run["state"] == "succeeded"

    async def test_retry_exhausted_fails_instance(self) -> None:
        async def always_down(*_a: object, **_k: object) -> dict:
            raise RuntimeError("持续不可用")

        engine = make_engine(WorkflowServices(http_call=always_down))
        workflow_id = await make_definition(
            engine,
            [node("call", "api_call", {"method": "GET", "url": "x"}, retry={"max_attempts": 1, "backoff_ms": 1})],
            [],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "6" * 16, payload={})
        assert detail["status"] == "failed"
        assert detail["nodes"][0]["attempts"] == 2
        assert "持续不可用" not in (detail["nodes"][0]["error"] or "")  # 引擎归一为类型化错误信息
        assert detail["nodes"][0]["error"]

    async def test_zero_retry_policy_runs_single_attempt(self) -> None:
        """边界：max_attempts=0 表示不重试，首次失败即定论（不再回到 handler）。"""
        calls = {"n": 0}

        async def boom(*_a: object, **_k: object) -> dict:
            calls["n"] += 1
            raise RuntimeError("一次性失败")

        engine = make_engine(WorkflowServices(http_call=boom))
        workflow_id = await make_definition(
            engine,
            [node("call", "api_call", {"method": "GET", "url": "x"}, retry={"max_attempts": 0, "backoff_ms": 0})],
            [],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "f0" * 8, payload={})
        assert calls["n"] == 1
        assert detail["nodes"][0]["attempts"] == 1
        assert detail["nodes"][0]["state"] == "failed"

    async def test_degrade_policy_continues(self) -> None:
        async def boom(*_a: object, **_k: object) -> dict:
            raise RuntimeError("智能体不可达")

        engine = make_engine(WorkflowServices(http_call=boom))
        workflow_id = await make_definition(
            engine,
            [
                node("call", "api_call", {"method": "GET", "url": "x"}, on_failure="degrade", retry={"max_attempts": 0}),
                node("after", "delay", {"seconds": 0}),
            ],
            [edge("call", "after")],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "7" * 16, payload={})
        states = {n["node_id"]: n["state"] for n in detail["nodes"]}
        assert states["call"] == "degraded"
        assert states["after"] == "succeeded"
        assert detail["status"] == "succeeded"

    async def test_abort_policy_stops_instance(self) -> None:
        async def boom(*_a: object, **_k: object) -> dict:
            raise RuntimeError("致命错误")

        engine = make_engine(WorkflowServices(http_call=boom))
        workflow_id = await make_definition(
            engine,
            [
                node("call", "api_call", {"method": "GET", "url": "x"}, on_failure="abort", retry={"max_attempts": 0}),
                node("next", "delay", {"seconds": 0}),
            ],
            [edge("call", "next")],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "8" * 16, payload={})
        assert detail["status"] == "failed"
        assert next(n for n in detail["nodes"] if n["node_id"] == "next")["state"] == "pending"

    async def test_timeout_is_classified_and_measured(self) -> None:
        async def hang(*_a: object, **_k: object) -> dict:
            await asyncio.sleep(1.0)
            return {}

        engine = make_engine(WorkflowServices(http_call=hang))
        workflow_id = await make_definition(
            engine,
            [
                node(
                    "slow",
                    "api_call",
                    {"method": "POST", "url": "x"},
                    timeout_ms=150,
                    sla_ms=150,
                    on_failure="skip",
                    retry={"max_attempts": 0},
                )
            ],
            [],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "9" * 16, payload={})
        run = detail["nodes"][0]
        assert "超时" in (run["error"] or "")
        assert run["state"] == "skipped"
        assert engine._tracer.ledger.stats("workflow_reschedule_ms").count == 1

    async def test_reschedule_latency_within_ten_seconds(self) -> None:
        """考核指标 2：异常工况识别与重调度响应 ≤10s（此处为失败→处置完成的实测时长）。"""
        attempts = {"n": 0}

        async def flaky(*_a: object, **_k: object) -> dict:
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RuntimeError("瞬时故障")
            return {"ok": True}

        engine = make_engine(WorkflowServices(http_call=flaky))
        workflow_id = await make_definition(
            engine,
            [node("call", "api_call", {"method": "GET", "url": "x"}, retry={"max_attempts": 3, "backoff_ms": 10})],
            [],
        )
        await engine.start(workflow_id, trace_id="trc_" + "a" * 16, payload={})
        stats = engine._tracer.ledger.stats("workflow_reschedule_ms")
        assert stats.count == 1
        assert stats.max <= 10_000, f"重调度实测 {stats.max:.0f}ms 超阈值"

    async def test_schedule_latency_within_two_seconds(self) -> None:
        async def query(**_k: object) -> list[dict]:
            return []

        engine = make_engine(WorkflowServices(telemetry_query=query))
        nodes = [node("root", "delay", {"seconds": 0})] + [node(f"n{i}", "data_fetch", {"limit": 1}) for i in range(20)]
        edges = [edge("root", f"n{i}") for i in range(20)]
        workflow_id = await make_definition(engine, nodes, edges)
        await engine.start(workflow_id, trace_id="trc_" + "b" * 16, payload={})
        stats = engine._tracer.ledger.stats("workflow_schedule_ms")
        assert stats.count >= 20
        assert stats.p95 <= 2_000, f"调度响应 P95 {stats.p95:.1f}ms 超阈值"


class TestHumanInTheLoop:
    async def test_pause_and_resume_approve(self) -> None:
        async def notify(_p: dict) -> None:
            return None

        async def query(**_k: object) -> list[dict]:
            return [{"metric": "rain_10min", "value": 40.0, "quality_flag": "ok"}]

        async def assess(payload: dict) -> dict:
            return {"risk_level": 2, "confidence": 0.8, "rationale": "测试", "hazard_type": "debris_flow", "region_code": "540121"}

        async def generate(payload: dict) -> dict:
            return {"warning_id": "wrn_test", "risk_level": 2, "channels": ["sms"], "region_codes": ["540121"]}

        async def publish(payload: dict) -> dict:
            return {"delivered": 1, "warning_id": payload["warning"]["warning_id"]}

        engine = make_engine(
            WorkflowServices(
                telemetry_query=query,
                assess=assess,
                generate_warning=generate,
                publish_warning=publish,
                notify=notify,
            )
        )
        workflow_id = await make_definition(
            engine,
            [
                node("fetch", "data_fetch", {"limit": 3}),
                node("judge", "threshold", {"upstream": "fetch", "conditions": [{"metric": "rain_10min", "op": ">=", "threshold": 30.0}]}),
                # warning_publish 必须有上游产出 warning 对象，故会签前先接 warning_generate（与内置模板一致的编排）。
                node("generate", "warning_generate", {}),
                node("sign", "human_review", {"prompt": "是否发布", "options": ["approve", "reject"]}, on_failure="escalate"),
                node("publish", "warning_publish", {"channels": ["sms"]}),
                node("reject_notify", "notify", {"text": "已驳回"}),
            ],
            [
                edge("fetch", "judge"),
                edge("judge", "generate", "triggered"),
                edge("generate", "sign"),
                edge("sign", "publish", "approve"),
                edge("sign", "reject_notify", "reject"),
            ],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "c" * 16, payload={})
        assert detail["status"] == "waiting"
        waiting = next(n for n in detail["nodes"] if n["state"] == "awaiting_human")
        assert waiting["node_id"] == "sign"

        resumed = await engine.resume(detail["instance_id"], node_id="sign", decision={"choice": "approve", "by": "指挥员"})
        states = {n["node_id"]: n["state"] for n in resumed["nodes"]}
        assert states["publish"] == "succeeded"
        assert states["reject_notify"] == "skipped"
        assert resumed["status"] == "succeeded"

    async def test_resume_with_invalid_choice_fails(self) -> None:
        engine = make_engine()
        workflow_id = await make_definition(engine, [node("sign", "human_review", {})], [])
        detail = await engine.start(workflow_id, trace_id="trc_" + "d" * 16, payload={})
        with pytest.raises(Exception) as info:
            await engine.resume(detail["instance_id"], node_id="sign", decision={"choice": "maybe"})
        assert "非法" in str(info.value)

    async def test_resume_non_waiting_node_rejected(self) -> None:
        engine = make_engine()
        workflow_id = await make_definition(engine, [node("a", "delay", {"seconds": 0})], [])
        detail = await engine.start(workflow_id, trace_id="trc_" + "e" * 16, payload={})
        with pytest.raises(WorkflowValidationError, match="未在等待"):
            await engine.resume(detail["instance_id"], node_id="a", decision={"choice": "approve"})

    async def test_bypass_unblocks_waiting_instance(self) -> None:
        async def notify(_p: dict) -> None:
            return None

        engine = make_engine(WorkflowServices(notify=notify))
        workflow_id = await make_definition(
            engine,
            [node("sign", "human_review", {}), node("after", "notify", {"text": "完成"})],
            [edge("sign", "after")],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "f" * 16, payload={})
        assert detail["status"] == "waiting"
        after = await engine.bypass_node(detail["instance_id"], "sign", reason="值班电话确认")
        assert {n["node_id"]: n["state"] for n in after["nodes"]}["after"] == "succeeded"

    async def test_bypassed_node_is_not_skipped_state(self) -> None:
        """旁路终态为 bypassed（区别于分支未命中产生的 skipped），以便下游被放行而非级联跳过。"""
        engine = make_engine()
        workflow_id = await make_definition(engine, [node("sign", "human_review", {})], [])
        detail = await engine.start(workflow_id, trace_id="trc_" + "f1" * 8, payload={})
        after = await engine.bypass_node(detail["instance_id"], "sign")
        assert next(n for n in after["nodes"] if n["node_id"] == "sign")["state"] == "bypassed"

    async def test_join_with_one_branch_bypassed(self) -> None:
        """双分支汇聚，其中一路被旁路：汇聚节点在等待期保持 pending，旁路后合并两路成功。"""

        async def query(**_k: object) -> list[dict]:
            return []

        engine = make_engine(WorkflowServices(telemetry_query=query))
        workflow_id = await make_definition(
            engine,
            [
                node("root", "delay", {"seconds": 0}),
                node("left", "data_fetch", {"limit": 1}),
                node("right", "human_review", {}),
                node("merge", "join", {"upstream": ["left", "right"]}),
            ],
            [edge("root", "left"), edge("root", "right"), edge("left", "merge"), edge("right", "merge")],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "f2" * 8, payload={})
        states = {n["node_id"]: n["state"] for n in detail["nodes"]}
        assert states["right"] == "awaiting_human"
        assert states["merge"] == "pending", "上游等待人工时汇聚节点须保持 pending，不得被跳过"

        after = await engine.bypass_node(detail["instance_id"], "right", reason="电话放行")
        astates = {n["node_id"]: n["state"] for n in after["nodes"]}
        assert astates["right"] == "bypassed"
        assert astates["merge"] == "succeeded"
        assert after["status"] == "succeeded"
        merge = next(n for n in after["nodes"] if n["node_id"] == "merge")
        assert set(merge["output"]["merged"]) == {"left", "right"}

    async def test_resume_on_pending_downstream_rejected(self) -> None:
        """只有真正等待决策的节点可 resume：其下游 pending 节点 resume 应被拒绝。"""
        engine = make_engine()
        workflow_id = await make_definition(
            engine, [node("sign", "human_review", {}), node("tail", "delay", {"seconds": 0})], [edge("sign", "tail")]
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "f3" * 8, payload={})
        assert next(n for n in detail["nodes"] if n["node_id"] == "tail")["state"] == "pending"
        with pytest.raises(WorkflowValidationError, match="未在等待"):
            await engine.resume(detail["instance_id"], node_id="tail", decision={"choice": "approve"})


class TestRuntimeFlexibility:
    async def test_update_pending_node_config_takes_effect(self) -> None:
        """运行中改参：实例尚未执行到的节点可改，且新参数生效（"柔性"的第三处体现）。"""
        seen: list[dict] = []

        async def notify(payload: dict) -> None:
            seen.append(payload)

        engine = make_engine(WorkflowServices(notify=notify))
        workflow_id = await make_definition(
            engine,
            [
                node("gate", "human_review", {}),
                node("alert", "notify", {"text": "原文案", "level": "info"}),
            ],
            [edge("gate", "alert")],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "0" * 16, payload={})
        assert detail["status"] == "waiting"

        patched = await engine.update_node_config(detail["instance_id"], "alert", {"text": "改后文案"})
        assert patched["config"]["text"] == "改后文案"

        resumed = await engine.resume(detail["instance_id"], node_id="gate", decision={"choice": "approve"})
        assert resumed["status"] == "succeeded"
        assert seen and seen[-1]["text"] == "改后文案"

    async def test_update_config_after_execution_rejected(self) -> None:
        engine = make_engine()
        workflow_id = await make_definition(engine, [node("a", "delay", {"seconds": 0})], [])
        detail = await engine.start(workflow_id, trace_id="trc_" + "1" * 16, payload={})
        with pytest.raises(WorkflowValidationError, match="不可改参"):
            await engine.update_node_config(detail["instance_id"], "a", {"seconds": 1})

    async def test_改参按键合并_没进载荷的键保留旧值(self) -> None:
        """界面上"清空那一格"等于这个键不再出现在载荷里，而这里是 `{**旧, **新}`：
        旧值原地留着、HTTP 200、画布显示空——三件事凑在一起就是"看着改成功了"。
        合并本身是有意的（改一个键不必回传整张图），所以这条钉住**事实**，
        前端的提示文案据此写；哪天改成能表达删除，这条会红，那时该一起改文案。"""
        seen: list[dict] = []

        async def notify(payload: dict) -> None:
            seen.append(payload)

        engine = make_engine(WorkflowServices(notify=notify))
        workflow_id = await make_definition(
            engine,
            [node("gate", "human_review", {}), node("alert", "notify", {"text": "原文案", "level": "info"})],
            [edge("gate", "alert")],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "4" * 16, payload={})
        assert detail["status"] == "waiting"

        patched = await engine.update_node_config(detail["instance_id"], "alert", {"level": "warning"})
        assert patched["config"] == {"text": "原文案", "level": "warning"}, "改一个键不该把另一个键顶掉"

        resumed = await engine.resume(detail["instance_id"], node_id="gate", decision={"choice": "approve"})
        alert_run = next(row for row in resumed["nodes"] if row["node_id"] == "alert")
        assert any("['level']" in note for note in alert_run["notes"]), "台账记的是这次发了哪些键，不是最终配置"
        assert seen[-1] == {"level": "warning", "text": "原文案", "payload": {}}

    async def test_insert_node_requires_existing_anchor(self) -> None:
        engine = make_engine()
        workflow_id = await make_definition(engine, [node("a", "delay", {"seconds": 0})], [])
        detail = await engine.start(workflow_id, trace_id="trc_" + "2" * 16, payload={})
        with pytest.raises(WorkflowValidationError, match="锚点节点不存在"):
            await engine.insert_node(detail["instance_id"], after="ghost", node=node("b", "delay", {"seconds": 0}))

    async def test_abort_marks_pending_nodes_cancelled(self) -> None:
        engine = make_engine()
        workflow_id = await make_definition(
            engine, [node("sign", "human_review", {}), node("tail", "delay", {"seconds": 0})], [edge("sign", "tail")]
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "3" * 16, payload={})
        aborted = await engine.abort(detail["instance_id"], reason="误报解除")
        assert aborted["status"] == "aborted"
        assert next(n for n in aborted["nodes"] if n["node_id"] == "tail")["state"] == "cancelled"

    async def test_insert_node_after_completed_upstream(self) -> None:
        """运行中改图：向"上游已完成、下游在等待"的在途实例插入节点，插入节点仍需被执行。"""
        seen: list[dict] = []

        async def notify(payload: dict) -> None:
            seen.append(payload)

        engine = make_engine(WorkflowServices(notify=notify))
        workflow_id = await make_definition(
            engine,
            [node("root", "delay", {"seconds": 0}), node("gate", "human_review", {}), node("tail", "delay", {"seconds": 0})],
            [edge("root", "gate"), edge("gate", "tail")],
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "20" * 8, payload={})
        states = {n["node_id"]: n["state"] for n in detail["nodes"]}
        assert states["root"] == "succeeded" and states["gate"] == "awaiting_human"

        await engine.insert_node(detail["instance_id"], after="root", node=node("inject", "notify", {"text": "补采"}))
        resumed = await engine.resume(detail["instance_id"], node_id="gate", decision={"choice": "approve"})
        rstates = {n["node_id"]: n["state"] for n in resumed["nodes"]}
        assert rstates["inject"] == "succeeded"
        assert rstates["gate"] == "succeeded" and rstates["tail"] == "succeeded"
        assert resumed["status"] == "succeeded"
        assert seen and seen[-1]["text"] == "补采"

    async def test_bypass_succeeded_node_rejected(self) -> None:
        """边界：已成功的节点不可再旁路（旁路只针对未执行/等待中的节点）。"""
        engine = make_engine()
        workflow_id = await make_definition(engine, [node("a", "delay", {"seconds": 0})], [])
        detail = await engine.start(workflow_id, trace_id="trc_" + "21" * 8, payload={})
        assert next(n for n in detail["nodes"] if n["node_id"] == "a")["state"] == "succeeded"
        with pytest.raises(WorkflowValidationError, match="不可旁路"):
            await engine.bypass_node(detail["instance_id"], "a")

    async def test_abort_is_idempotent(self) -> None:
        """边界：中止可重复调用，第二次为空操作，不覆盖首次原因、不改变节点终态。"""
        engine = make_engine()
        workflow_id = await make_definition(
            engine, [node("sign", "human_review", {}), node("tail", "delay", {"seconds": 0})], [edge("sign", "tail")]
        )
        detail = await engine.start(workflow_id, trace_id="trc_" + "22" * 8, payload={})
        first = await engine.abort(detail["instance_id"], reason="误报解除")
        second = await engine.abort(detail["instance_id"], reason="重复中止")
        assert first["status"] == "aborted" and second["status"] == "aborted"
        assert second["error"] == "误报解除"
        first_states = {n["node_id"]: n["state"] for n in first["nodes"]}
        second_states = {n["node_id"]: n["state"] for n in second["nodes"]}
        assert first_states == second_states == {"sign": "cancelled", "tail": "cancelled"}

    async def test_instance_isolation_between_runs(self) -> None:
        engine = make_engine()
        workflow_id = await make_definition(engine, [node("a", "delay", {"seconds": 0})], [])
        first = await engine.start(workflow_id, trace_id="trc_" + "4" * 16, payload={"x": 1})
        second = await engine.start(workflow_id, trace_id="trc_" + "5" * 16, payload={"x": 2})
        assert first["instance_id"] != second["instance_id"]
        assert len(engine.instances()) == 2

    async def test_start_unknown_workflow(self) -> None:
        engine = make_engine()
        with pytest.raises(WorkflowValidationError, match="不存在"):
            await engine.start("wf_" + "z" * 12, trace_id="trc_" + "6" * 16)

    async def test_instance_detail_missing_returns_none(self) -> None:
        engine = make_engine()
        assert engine.instance_detail("wfi_" + "0" * 12) is None


async def _wait_running_instance(engine: WorkflowEngine, node_id: str = "") -> str:
    """轮询到实例出现（可选：某个节点已进入 running），返回 instance_id。

    中止类用例必须挑"handler 真的在跑"的那一刻下手：`start()` 返回时实例要么已终态、
    要么在等人，拿不到执行中途。指定 node_id 时等它变 running，避免靠 sleep 猜时序。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while loop.time() < deadline:
        for row in engine.instances():
            if not node_id:
                return row["instance_id"]
            states = {n["node_id"]: n["state"] for n in row["nodes"]}
            if states.get(node_id) == "running":
                return row["instance_id"]
        await asyncio.sleep(0.01)
    raise AssertionError(f"等待实例进入 running 超时: {node_id or '(任意节点)'}")


class TestAbortDuringExecution:
    """中止必须停住动作，而不只是把状态改成 aborted。

    旧实现只改 `instance.status`，在跑的 handler 照跑：动作发完之后 `_finalise` 又把实例
    从 ABORTED 改写成 SUCCEEDED——屏幕上写"已中止"，通知其实已经发出去了。
    """

    async def test_中止运行中实例后不会被改写为成功(self) -> None:
        seen: list[dict] = []

        async def notify(payload: dict) -> None:
            seen.append(payload)

        engine = make_engine(WorkflowServices(notify=notify))
        workflow_id = await make_definition(
            engine,
            [node("slow", "delay", {"seconds": 0.6}), node("alert", "notify", {"text": "事后通知"})],
            [edge("slow", "alert")],
        )
        start_task = asyncio.create_task(engine.start(workflow_id, trace_id="trc_" + "23" * 8, payload={}))
        instance_id = await _wait_running_instance(engine, "slow")
        aborted = await engine.abort(instance_id, reason="误报解除")
        detail = await asyncio.wait_for(start_task, timeout=2)

        assert aborted["status"] == "aborted"
        assert detail["status"] == "aborted", "中止先到，收尾不得把实例改写成 succeeded"
        assert {n["node_id"]: n["state"] for n in detail["nodes"]} == {"slow": "cancelled", "alert": "cancelled"}
        assert seen == [], "中止之后下游通知不许再发出去"

    async def test_中止后排队等信号量的节点不执行(self) -> None:
        """并发闸限 1：第二个根节点还在等信号量，中止必须让它连 handler 都不进。"""
        calls: list[str] = []

        async def http_call(method: str, url: str, body: object = None) -> dict:
            calls.append(url)
            return {"ok": True}

        engine = make_engine(WorkflowServices(http_call=http_call), max_parallel=1)
        workflow_id = await make_definition(
            engine,
            [
                node("slow", "delay", {"seconds": 0.6}),
                node("after_slow", "notify", {"text": "慢支路"}),
                node("queued", "api_call", {"method": "GET", "url": "queued"}),
                node("after_queued", "notify", {"text": "排队支路"}),
            ],
            [edge("slow", "after_slow"), edge("queued", "after_queued")],
        )
        start_task = asyncio.create_task(engine.start(workflow_id, trace_id="trc_" + "24" * 8, payload={}))
        instance_id = await _wait_running_instance(engine, "slow")
        await engine.abort(instance_id, reason="误报解除")
        detail = await asyncio.wait_for(start_task, timeout=2)

        states = {n["node_id"]: n["state"] for n in detail["nodes"]}
        assert states["slow"] == "cancelled"
        assert states["queued"] == "cancelled"
        assert calls == [], "排队节点在中止后仍拿到了并发闸并执行了动作"

    async def test_同一实例同一时刻只有一条驱动循环(self) -> None:
        """bypass 在实例 RUNNING 时会再起一条驱动；没有实例锁，两条循环会同时
        `_collect_ready` 到同一批节点，把同一个 handler 执行两遍。

        判据取"bypass 必须等主驱动跑完"：实例锁在时 `bypass_node` 会阻塞到 0.3s 的两根
        都结束；没有锁它毫秒级返回（顺手把下游通知发了）。
        """
        seen: list[dict] = []

        async def notify(payload: dict) -> None:
            seen.append(payload)

        engine = make_engine(WorkflowServices(notify=notify))
        workflow_id = await make_definition(
            engine,
            [
                node("left", "delay", {"seconds": 0.3}),
                node("right", "delay", {"seconds": 0.3}),
                node("bridge", "delay", {"seconds": 0}),
                node("alert", "notify", {"text": "只发一次"}),
            ],
            [edge("right", "bridge"), edge("bridge", "alert")],
        )
        start_task = asyncio.create_task(engine.start(workflow_id, trace_id="trc_" + "25" * 8, payload={}))
        instance_id = await _wait_running_instance(engine, "left")
        bypass_task = asyncio.create_task(engine.bypass_node(instance_id, "bridge", reason="不必要"))

        await asyncio.sleep(0.05)
        assert not bypass_task.done(), "实例锁失效：bypass 在主驱动仍在跑时就另起了一条驱动循环"

        detail = await asyncio.wait_for(start_task, timeout=3)
        await asyncio.wait_for(bypass_task, timeout=2)
        assert detail["status"] == "succeeded"
        assert len(seen) == 1, "同一个通知节点被执行了不止一次"


def _trace(i: int) -> str:
    return "trc_" + format(i, "016x")


class TestInstanceRetention:
    """留底上界：实例只活在内存里（落库面未接），D7 之后每条预警都起一个实例，无上界就是常驻泄漏。"""

    async def test_超出上界挤掉最老的终态实例(self) -> None:
        engine = make_engine(max_instances=8)
        workflow_id = await make_definition(engine, [node("a", "delay", {"seconds": 0})], [])
        details = [await engine.start(workflow_id, trace_id=_trace(i), payload={}) for i in range(12)]
        assert len(engine.instances()) == 8, "上界没压住：驻留实例还在涨"
        assert engine.evicted_instances == 4
        kept = {row["instance_id"] for row in engine.instances()}
        assert details[-1]["instance_id"] in kept, "最新一条必须还在，值班员刚跑完就要看它"
        assert engine.instance_detail(details[0]["instance_id"]) is None

    async def test_等待人工的实例不被上界挤掉(self) -> None:
        """WAITING 是现场工单：不能因为后来跑了 11 条预警，值班员手上的图就 404。

        这条是"只挤终态"与"按最老挤"的分水岭——前者是本引擎的判据，后者会假绿在这条用例上。
        """
        engine = make_engine(max_instances=8)
        sign_id = await make_definition(engine, [node("sign", "human_review", {})], [])
        waiting = await engine.start(sign_id, trace_id=_trace(0), payload={})
        assert waiting["status"] == "waiting"
        plain_id = await make_definition(engine, [node("a", "delay", {"seconds": 0})], [])
        for i in range(1, 12):
            await engine.start(plain_id, trace_id=_trace(i), payload={})
        assert waiting["instance_id"] in {row["instance_id"] for row in engine.instances()}
        resumed = await engine.resume(waiting["instance_id"], node_id="sign", decision={"choice": "approve"})
        assert resumed["status"] == "succeeded"

    async def test_上界过小在构造期拒绝(self) -> None:
        with pytest.raises(ValueError, match="不得小于 8"):
            make_engine(max_instances=4)
