"""考核指标实测报告：把运行时埋点分位数与课题 SLA 阈值并列输出。

原则：报告里的每个数字都来自本次运行的真实样本（计数为 0 即如实标注未测得），
不写"应达到"的期望值。未由本平台数据集覆盖的指标（如预警准确率）显式标记 not_measured。

预警准确率只能从标注案例回放里算，不来自本次在线运行：`--dataset` 给出 JSONL 时
由 `aegis.persistence.replay` 口径唯一地算出（报表侧不重算），不给就如实写"未测得"。
数据集必须是现场标注（`{"dataset":{"kind":"field"}}`）才可能被判"达标"——
合成样例的结论永远是 `synthetic_only`，它只证明算术，不证明指标。

用法：
    uv run python -m scripts.metrics_report --rounds 20 --agents
    uv run python -m scripts.metrics_report --rounds 50 --out reports/metrics.json
    uv run python -m scripts.metrics_report --dataset /data/aegis/labels.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from aegis.bus.transport import drain_pending
from aegis.config import Settings
from aegis.connectors.simulator import HazardScenarioSimulator
from aegis.container import create_container
from aegis.domain.messages import utc_now
from aegis.observability.tracer import unit_of_metric
from aegis.persistence.replay import ReplayReport, load_dataset, measure

ACCURACY_INDICATOR = "多灾种灾害预警准确率 ≥80%"
TYPE_ACCURACY_INDICATOR = "5 类灾种触发条件识别准确率"

#: 指标名 → 对应考核口径。阈值不在这里抄第二份：账本出口自带 `budget` 与 `unit`，
#: 唯一的登记处是 `instrumentation.register_sla_budgets`。此前这份副本按"毫秒"写死，
#: 秒制那两条只能另开一支循环处理（`INGEST_INDICATOR` / `REPORT_INTAKE_INDICATOR`），
#: 而"另开一支"正是当年把秒值拿去比毫秒阈值、永远绿的源头。
#: 每个量测点属于哪一口径（§5 A 案三档）：调度档 / LLM 档 / 接入档 / 端到端档
TIER_OF_METRIC: dict[str, str] = {
    "sync_agent_to_gateway_ms": "共享档",
    "workflow_schedule_ms": "调度档",
    "workflow_node_ms": "调度档",
    "workflow_reschedule_ms": "调度档",
    "collab_txn": "LLM 档",
    "stage_assess_ms": "LLM 档",
    "stage_execute_ms": "LLM 档",
    "stage_feedback_ms": "LLM 档",
    "stage_plan_ms": "LLM 档（诊断）",
    "stage_perceive_ms": "LLM 档（诊断）",
    "llm_call_ms": "LLM 档（单次调用）",
    "warning_generation_ms": "端到端档",
    "warning_reach_ms": "端到端档",
    "assistant_reply_ms": "端到端档",
    "ingest_end_to_end_seconds": "接入档",
    "report_intake_seconds": "接入档",
}

INDICATORS: dict[str, str] = {
    "sync_agent_to_gateway_ms": "多节点数据共享同步时延 ≤3s",
    # 语义交互与数据共享同属考核指标 3 的 ≤3s 一档（`services/assistant.py` 唯一的记账点）
    "assistant_reply_ms": "语义交互一轮响应 ≤3s",
    "collab_txn": "协同事务响应 ≤60s（LLM 档：含模型往返）",
    # ≤10s 那条考核由调度口径代表（重调度禁入 LLM，§3.3 / §5 A 案）；跑不到样本就如实"未测得"
    "workflow_reschedule_ms": "异常工况识别与重调度响应 ≤10s（调度口径）",
    # ≤2s 这条考核项的定义量测点是"节点就绪→开始执行"（见 `workflow/engine.py:5` 与
    # `instrumentation.register_sla_budgets`），所以表里代表它的那一行必须是 workflow_schedule_ms。
    # 链路阶段时延沿用同一档预算，但只作诊断行：把"研判耗时"挂成"调度响应"来判达标，
    # 审的人按定义一问就穿（真 LLM 跑一次还出现过两行同名、判定相反的表）。
    "workflow_schedule_ms": "常规任务调度响应时延 ≤2s",
    # §5 口径分档：这几行走 LLM 档（各 ≤60s，卷入预警生成 ≤3min 端到端），不再是"沿用 ≤2s 的诊断行"
    "stage_assess_ms": "研判段端到端 ≤60s（LLM 档）",
    "stage_execute_ms": "执行段端到端 ≤60s（LLM 档：三分支文案+翻译）",
    "stage_feedback_ms": "反馈段端到端 ≤60s（LLM 档：归因）",
    "stage_plan_ms": "决策段时延（诊断行，不设 LLM 档：缺网关时走本地剧本）",
    "stage_perceive_ms": "感知段时延（诊断行，不设 LLM 档：语义腿为委派）",
    "warning_generation_ms": "预警信息生成时间 ≤3min",
    "warning_reach_ms": "预警信息靶向触达 ≤20min",
    "ingest_end_to_end_seconds": "多源数据接入时延 ≤5分钟",
    # 人工上报是"接入 ≤5min"的第四条腿（批次 B4）
    "report_intake_seconds": "人工上报接入时延 ≤5分钟（第四条腿）",
}

#: 固定两条上报文本：一条够得上阈值（该报警），一条只描述现象（不该报警）。
#: 用固定文本而不是随机句子，是为了让"报表里的 20 例"每次都能被逐条复跑对上。
REPORT_SAMPLES: tuple[tuple[str, str], ...] = (
    ("24小时累计降雨95毫米，沟道泥位抬升1.2米，下游约300人受威胁", "540121"),
    ("坡面出现裂缝并有石块滚落，暂无其它数据", "540200"),
)

ACCURACY_HINT = "需 5 灾种标注案例回放：`--dataset <labels.jsonl>`（现场标注口径见 persistence/replay.py）"

NOT_MEASURED = {
    ACCURACY_INDICATOR: ACCURACY_HINT,
    TYPE_ACCURACY_INDICATOR: "需案例标注测试集，本次运行仅覆盖规则引擎可判定的触发组合",
}


def _judgement(report: ReplayReport) -> str:
    """判定词按 (status, indicator) 两轴给：样本不足与没给数据是两种结论，不能共用一句话。"""
    if report.status == "insufficient_sample":
        return f"现场标注样本不足（<{report.min_field_cases} 例，本次 {report.confusion.total} 例）"
    if report.indicator == "not_measured":
        return "未测得（未提供数据集或数据集为空）"
    if report.indicator == "synthetic_only":
        return "仅合成数据集：算术已验证，不构成官方口径证据"
    return "达标" if report.indicator == "met" else "未达标"


def accuracy_rows(report: ReplayReport) -> list[dict[str, Any]]:
    """准确率两行：判定词与官方口径分开给，"报了"与"报对灾种"也分开给。"""
    overall = report.confusion
    judgement = _judgement(report)
    rows: list[dict[str, Any]] = [
        {
            "指标": ACCURACY_INDICATOR,
            "样本": overall.total,
            "准确率": overall.accuracy,
            "精确率": overall.precision,
            "召回率": overall.recall,
            "F1": overall.f1,
            "计数": {"tp": overall.tp, "fp": overall.fp, "fn": overall.fn, "tn": overall.tn},
            "阈值": report.target,
            "官方口径准确率": report.official_accuracy,
            "判定": judgement,
            "数据集": {"kind": report.dataset_kind, "source": report.dataset_source, "note": report.dataset_note},
        }
    ]
    if overall.tp:
        rows.append(
            {
                "指标": TYPE_ACCURACY_INDICATOR,
                "样本": overall.tp,
                "灾种报对": report.type_correct,
                "判定": f"{report.type_correct / overall.tp:.3f}",
            }
        )
    else:
        # 灾种是否报对只对真阳性有定义：一条都没报出来时无从判断，不是 0%
        rows.append({"指标": TYPE_ACCURACY_INDICATOR, "样本": 0, "判定": "未测得（无真阳性案例）"})
    return rows


def accuracy_report(dataset: Path | None, *, target: float | None = None) -> ReplayReport:
    """`--dataset` 缺省时也走同一份口径：空数据集 -> not_measured，而不是让报表自己造结论。"""
    loaded = None if dataset is None else load_dataset(dataset)
    return measure(loaded, target=target)


def _first_stat(metrics: dict[str, Any], name: str, key: str) -> float | None:
    """取某指标的一个分位数；埋点缺失或无样本一律 None。

    存在理由：报表里"0"与"没测到"必须是两件事。写成 `…get(key, 0.0)` 会把没跑过的腿
    报成"耗时 0 毫秒"，读报表的人会当成达标。
    """
    stats = metrics.get(name)
    if not isinstance(stats, dict) or not stats.get("count"):
        return None
    value = stats.get(key)
    return None if not isinstance(value, (int, float)) else float(value)


def _table(latency: dict[str, Any], collaboration: dict[str, Any]) -> list[dict[str, Any]]:
    """考核指标表：数值与阈值一律取自账本出口，判定只比较同单位的两个数。

    列名跟着单位走（`P95_ms` / `P95_s`），因为这张表里既有毫秒埋点也有秒制埋点——
    把两者塞进同一个 `_ms` 列名就是 1000 倍误读，硬凑一个不带单位的列头又让读者自己猜。
    出口自带的 `unit` 与指标名后缀若不一致，这里直接抛错：一份悄悄错 1000 倍的报表
    比一份没有报表更坏。
    """
    rows: list[dict[str, Any]] = []
    metrics = latency["metrics"]
    for name, description in INDICATORS.items():
        unit = unit_of_metric(name)
        stats = metrics.get(name)
        row: dict[str, Any] = {"指标": description, "口径档": TIER_OF_METRIC.get(name, "未归档"), "样本": 0, "单位": unit}
        for key in ("P50", "P95", "最大"):
            row[f"{key}_{unit}"] = None
        if stats is not None and stats.get("unit", unit) != unit:
            raise ValueError(f"{name}: 出口单位 {stats.get('unit')} 与指标名后缀 {unit} 不一致")
        budget = None if stats is None else stats.get("budget")
        row[f"阈值_{unit}"] = budget
        count = 0 if stats is None else stats["count"]
        row["样本"] = count
        if not count:
            row["判定"] = "未测得（账本里没有这条指标的样本）"
            rows.append(row)
            continue
        for source, label in (("p50", "P50"), ("p95", "P95"), ("max", "最大")):
            row[f"{label}_{unit}"] = stats[source]
        if budget is None:
            row["判定"] = "未设阈值"
        else:
            row["判定"] = "达标" if stats["p95"] <= budget else "超标"
            row["越限样本"] = stats.get("breaches", 0)
        rows.append(row)

    rate = collaboration["success_rate"]
    rows.append(
        {
            "指标": "多智能体协同联动成功率 ≥90%",
            # 成功率与 LLM 档时延必须并排看：分母就是这些事务，超时预算给 10s 还是 60s 直接决定它
            "口径档": "LLM 档",
            "样本": collaboration["transactions"],
            "成功率": rate,
            "判定": ("达标" if rate is not None and rate >= collaboration["target"] else "未达标")
            if rate is not None
            else "未测得（无协同事务）",
        }
    )
    return rows


def resolve_settings(
    *,
    bus_backend: str,
    store_backend: str,
    agent_runtime: str,
    seed: int,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """把"在哪种环境上量"落成配置。缺凭据就当场炸，绝不静默退回内存。

    内存总线 + 内存视图 + mock 替身跑出来的数只能做回归对照；把它们当成 §6 的出口数字，
    是这批报表最容易犯、也最难事后发现的一种假话。
    """
    env = os.environ if environ is None else environ
    if bus_backend == "nats" and not (env.get("AEGIS_NATS_URL") or "").strip():
        raise ValueError("--bus nats 需要 AEGIS_NATS_URL（服务地址见 deploy/docker-compose.yml）")
    if store_backend == "postgres" and not (env.get("AEGIS_PG_DSN") or "").strip():
        raise ValueError("--store postgres 需要 AEGIS_PG_DSN（指向 compose 暴露的 127.0.0.1:5432）")
    return Settings(
        env="dev",
        bus_backend=bus_backend,
        store_backend=store_backend,
        agent_runtime=agent_runtime,
        delivery_mode="mock",
        simulator_seed=seed,
        nats_url=(env.get("AEGIS_NATS_URL") or "").strip(),
        pg_dsn=(env.get("AEGIS_PG_DSN") or "").strip(),
    )


def mixed_registrations(snapshot: Sequence[Mapping[str, Any]]) -> list[str]:
    """找出"同一类型既有 mock 替身又有真内环在册"的类型——两副身子会抢答同一批请求。

    实测踩过的那次：后端容器没设 AEGIS_AGENT_RUNTIME（默认 mock）与主机 `--runtime real` 连着
    同一条 JetStream，于是 assess/plan 被容器里的 mock 毫秒级应答，主机的真内环同时也收到并在
    烧 token——报表里 `mode=agent` 与段时延两边都不代表"agent 模式"的真实口径。
    进程内的互斥开关看不见这一幕，所以判据必须落在总线视角的在册表上。
    识别用的是 id 后缀约定（`*.mock01` / `*.real01`，平台两侧的默认命名）；认不出来就当作无风险
    也不误判达标——最坏的漏判由"可作出口数字"的另一半（真内环计数）兜住。
    """
    kinds: dict[str, set[str]] = {}
    for entry in snapshot:
        agent_id = str(entry.get("agent_id") or "")
        agent_type = str(entry.get("agent_type") or "")
        suffix = agent_id.partition(".")[2].lower()
        kind = "mock" if "mock" in suffix else "real" if "real" in suffix else ""
        if agent_type and kind:
            kinds.setdefault(agent_type, set()).add(kind)
    return sorted(agent_type for agent_type, seen in kinds.items() if len(seen) > 1)


def measurement_environment(
    settings: Settings,
    *,
    llm_configured: bool,
    real_agents: int,
    mock_agents: int,
    registry_snapshot: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """报表出口必须自带"这数是在什么上量出来的"，否则内存跑的数与真栈跑的数长得一模一样。"""
    full_stack = settings.bus_backend == "nats" and settings.store_backend == "postgres"
    contested = mixed_registrations(registry_snapshot)
    return {
        "总线": settings.bus_backend,
        "存储": settings.store_backend,
        "智能体运行时": settings.agent_runtime,
        "交付模式": settings.delivery_mode,
        "LLM 网关": "已配（时延含真模型调用）" if llm_configured else "未配（内环走降级轨，时延不代表真模型）",
        "在册智能体": {"真内环": real_agents, "mock 替身": mock_agents, "总线视角总数": len(registry_snapshot)},
        "抢答风险": contested,
        "可作出口数字": bool(full_stack and settings.agent_runtime == "real" and llm_configured and not contested),
        "口径提醒": (
            "本行数出自真栈（NATS + PostgreSQL + real 运行时 + 已配 LLM，且总线只有一副身子），可按 §6 口径引用"
            if full_stack and settings.agent_runtime == "real" and llm_configured and not contested
            else "本行数【不可】当作 §6 出口数字：需 --bus nats --store postgres --runtime real、LLM 已配，"
            "且总线上不得 mock 与 real 并存在抢答"
        ),
    }


async def workflow_leg(container: Any, *, rounds: int, names: Sequence[str] | None = None) -> dict[str, Any]:
    """工作流腿：让 `workflow_schedule_ms`（节点就绪→开始执行）真出样本——那是 ≤2s 考核项的定义量测点。

    为什么放在这里而不是 drill：批量口径要的是"seed 固定、轮次可控、JSON 出口"的可复现形态，
    drill 是场景演示工具，让它兼职产数会把两件事搞混（用户定的口径）。

    两条如实交代：
    - 灾种剧本以"指挥员核签"收尾。脚本代不了真人签字，这里用一条**显式标注 actor=metrics_report**
      的 approve 决策把这条腿跑到终态，并在报表里单列"脚本代签核签"计数——不加计数就等于把
      "无人签字的演练"读成"指挥员签过了"。
    - 模板没注册（部署面缺内置剧本）时不静默：把原因写进 `说明`，那一行考核项继续显示"未测得"。
    """
    from aegis.domain.messages import new_trace_id
    from aegis.workflow.templates import BUILTIN_TEMPLATES

    block: dict[str, Any] = {"实例": 0, "脚本代签核签": 0, "终态": {}, "说明": ""}
    if rounds <= 0:
        block["说明"] = "工作流腿未开（--workflow-rounds 0）"
        return block
    # None=用内置剧本；显式传空列表是"点名要跑但没有剧本"，必须如实停，不能悄悄换成内置清单跑两轮
    cycle = list(names) if names is not None else [str(template["name"]) for template in BUILTIN_TEMPLATES]
    if not cycle:
        block["说明"] = "没有可用模板名，工作流腿未跑"
        return block
    try:
        for index in range(rounds):
            detail = await container.workflow.start_from_name(
                cycle[index % len(cycle)], trace_id=new_trace_id(), payload={"source": "metrics_report"}
            )
            block["实例"] += 1
            pending = [node["node_id"] for node in detail.get("nodes", []) if node.get("state") == "awaiting_human"]
            if pending:
                detail = await container.workflow.resume(
                    detail["instance_id"],
                    node_id=str(pending[0]),
                    decision={"choice": "approve", "actor": "metrics_report", "reason": "批量口径脚本代签（非真人核签）"},
                )
                block["脚本代签核签"] += 1
            status = str(detail.get("status", "unknown"))
            block["终态"][status] = block["终态"].get(status, 0) + 1
    except Exception as exc:
        block["说明"] = f"工作流腿中断：{type(exc).__name__}: {exc}"
    return block


async def run(
    *,
    rounds: int,
    reports: int = 2,
    with_agents: bool,
    seed: int,
    out: Path | None,
    dataset: Path | None = None,
    accuracy_target: float | None = None,
    bus_backend: str = "memory",
    store_backend: str = "memory",
    agent_runtime: str = "mock",
    workflow_rounds: int | None = None,
) -> int:
    settings = resolve_settings(bus_backend=bus_backend, store_backend=store_backend, agent_runtime=agent_runtime, seed=seed)
    container = create_container(settings, with_simulator=False)
    # real 与 mock 互斥由容器保证；这里仍按开关请求，好让"开了真没起替身"这件事在容器里留下事实
    await container.start(with_mock_agents=with_agents)
    await asyncio.sleep(0.1)

    simulator = HazardScenarioSimulator(scenario="surge", seed=seed)
    acted = 0
    fatal = 0
    try:
        for _round in range(rounds):
            readings = simulator.collect_at(utc_now())
            grouped: dict[str, list[Any]] = {}
            for reading in readings:
                grouped.setdefault(reading.region_code, []).append(reading)
            results = await container.chain.process_many(grouped)
            acted += sum(1 for r in results if r.acted)
            fatal += sum(1 for r in results if r.errors)
            await drain_pending(container.transport, timeout=5.0)
        # 上报腿：两条固定文本各跑一遍（一条该报警、一条不该报），
        # 让"接入 ≤5min"这项在报表里有一条属于人工上报的独立样本，而不是只蹭遥测那条腿
        submitted = reviewed = with_warning = 0
        for index in range(reports):
            text, region = REPORT_SAMPLES[index % len(REPORT_SAMPLES)]
            outcome = await container.submit_report(note=text, region_code=region, reporter=f"metrics-report-{index + 1}")
            submitted += 1
            reviewed += 1 if outcome["human_review_required"] else 0
            with_warning += 1 if outcome["chain"]["warning_id"] else 0
            fatal += 1 if outcome["chain"]["errors"] else 0
            await drain_pending(container.transport, timeout=5.0)
        # 语义交互这条腿：只读查询走完整轮（意图解构→动作→认知镜像），让 ≤3s 这项在报表里
        # 有自己的样本。只发查询、不发执行类任务，因此不需要 /confirm，也不会有任何动作被执行。
        assistant_rounds = 0
        assistant = getattr(container, "assistant", None)
        if assistant is not None:
            for index in range(max(reports, 1)):
                async for _ in assistant.respond("查一下 540121 的预警", session_id=f"metrics-report-{index}"):
                    pass
                assistant_rounds += 1
        wf_block = await workflow_leg(container, rounds=rounds if workflow_rounds is None else workflow_rounds)
        latency = container.latency_report()
        env_block = measurement_environment(
            settings,
            llm_configured=container.llm_gateway is not None,
            real_agents=len(container.real_agents),
            mock_agents=len(container.mock_agents),
            registry_snapshot=container.registry.snapshot(),
        )
    finally:
        await container.shutdown()

    # 准确率不来自这次在线运行：没给数据集就只报"未量测"，不给它任何看起来像结论的形状
    accuracy = accuracy_report(dataset, target=accuracy_target)
    rows = _table(latency, latency["collaboration"])
    uncovered = dict(NOT_MEASURED)
    if dataset is not None:
        rows += accuracy_rows(accuracy)
        covered = {str(row["指标"]) for row in rows}
        uncovered = {name: hint for name, hint in NOT_MEASURED.items() if name not in covered}

    report = {
        "运行参数": {"rounds": rounds, "with_mock_agents": with_agents, "seed": seed, "事件产出": acted, "致命错误": fatal},
        "测量环境": env_block,
        "工作流腿": wf_block,
        "人工上报腿": {
            "受理": submitted,
            "产出预警": with_warning,
            "转人工核签": reviewed,
            "台账计数": latency.get("reports"),
            "阈值版本": latency.get("rulebook"),
        },
        "语义交互腿": {
            "轮次": assistant_rounds,
            # 没有样本就是 None（报表里显示"未测得"），不能拿 0.0 顶上去——那是一行"一轮耗时 0 ms"的假话
            "一轮耗时_ms": _first_stat(latency["metrics"], "assistant_reply_ms", "p50"),
            "说明": "只读查询，未做执行类确认；本机无 LLM 凭据时为「纯规则词表」口径" if assistant_rounds else "助手腿未装配",
        },
        "指标判定": rows,
        "准确率回放": accuracy.as_dict(),
        "未覆盖指标": uncovered,
        "原始分位": latency["metrics"],
        "网关计数": latency["gateway_counters"],
        "运行态": latency["store"],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        print(f"\n[已写入] {out}", file=sys.stderr)
    # 只有"现场标注数据集判出不达标"才算这次报表失败；未量测不是失败，是如实的空结论
    return 1 if fatal or accuracy.indicator in {"not_met", "insufficient_sample"} else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AEGIS 考核指标实测报告")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--reports", type=int, default=2, help="人工上报腿的样本条数（0 即不测这条腿，报表会写「未测得」）")
    parser.add_argument(
        "--agents", action="store_true", help="起 Mock 智能体（否则测平台降级路径；--runtime real 时容器会按互斥压制 mock）"
    )
    parser.add_argument("--bus", choices=("memory", "nats"), default="memory", help="总线：nats 需 AEGIS_NATS_URL")
    parser.add_argument("--store", choices=("memory", "postgres"), default="memory", help="存储视图：postgres 需 AEGIS_PG_DSN")
    parser.add_argument(
        "--runtime",
        choices=("mock", "real", "off"),
        default=None,
        help="智能体运行时（缺省沿用 .env 的 AEGIS_AGENT_RUNTIME）；real=真内环上总线（只产文案，不持发送权）",
    )
    parser.add_argument("--seed", type=int, default=202_609)
    parser.add_argument(
        "--workflow-rounds",
        type=int,
        default=None,
        help="工作流腿实例数（≤2s 考核项的定义量测点在此）；缺省跟 --rounds 同数，0 即关闭",
    )
    parser.add_argument("--out", type=Path, default=None, help="同时把报告写入 JSON 文件")
    parser.add_argument("--dataset", type=Path, default=None, help="预警准确率回放数据集（JSONL，口径见 persistence/replay.py）")
    parser.add_argument("--accuracy-target", type=float, default=None, help="覆盖配置里的准确率阈值（0—1]，仅影响判定行")
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds 必须 ≥ 1")
    if args.workflow_rounds is not None and args.workflow_rounds < 0:
        parser.error("--workflow-rounds 不得为负（要关闭请给 0）")
    if args.dataset is not None and args.dataset.is_file() is False:
        parser.error(f"--dataset 文件不存在：{args.dataset}")
    runtime = args.runtime or Settings(env="dev").agent_runtime
    return asyncio.run(
        run(
            rounds=args.rounds,
            reports=max(0, args.reports),
            with_agents=args.agents,
            seed=args.seed,
            out=args.out,
            dataset=args.dataset,
            accuracy_target=args.accuracy_target,
            bus_backend=args.bus,
            store_backend=args.store,
            agent_runtime=runtime,
            workflow_rounds=args.workflow_rounds,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
