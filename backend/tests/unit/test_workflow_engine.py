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

    async def test_parallel_branches_run_concurrently(self) -> None:
        async def slow_query(**_kwargs: object) -> list[dict]:
            await asyncio.sleep(0.12)
            return []

        engine = make_engine(WorkflowServices(telemetry_query=slow_query))
        workflow_id = await make_definition(
            engine,
            [
                node("root", "delay", {"seconds": 0}),
                node("left", "data_fetch", {"limit": 1}),
                node("right", "data_fetch", {"limit": 1}),
                node("join", "join", {"upstream": ["left", "right"]}),
            ],
            [edge("root", "left"), edge("root", "right"), edge("left", "join"), edge("right", "join")],
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        detail = await engine.start(workflow_id, trace_id="trc_" + "3" * 16, payload={})
        elapsed = loop.time() - started
        assert detail["status"] == "succeeded"
        join = next(n for n in detail["nodes"] if n["node_id"] == "join")
        assert set(join["output"]["merged"]) == {"left", "right"}
        assert elapsed < 0.22, f"并行分支疑似串行执行（耗时 {elapsed:.2f}s）"

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
