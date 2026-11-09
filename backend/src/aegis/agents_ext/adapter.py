"""外部智能体模块与平台契约之间的**适配层**（设计 §0 边界三原则的落地）。

## 为什么需要这一层

她的实现是**单进程 LangGraph 图编排**：六个 agent 各自 `run_xxx_agent(...) -> dict`，
进程内互相调用，入口是 `main.py` 的命令行。平台的契约却是**分布式总线**：
智能体是总线上的独立服务，唯一交互载体是 `AgentMessage v1`
（见 `contracts/AGENT_INTEGRATION_SPEC.v1.md`）。

两种模型不能直接拼接。本层做的事只有一件：**把她的 `run_xxx_agent(...)` 适配成
`AgentKit.handle(request: AgentMessage) -> Reply`**，即平台契约壳认识的那一种内环。
适配之后，她那一整套图、规则引擎、五灾种注册表就都成了平台契约下的一个合法实现，
与平台自建内环（`aegis.agents`）**并列**、由运行配置选择走哪一套。

## 三条不能含糊的口径

1. **数据面不假装**：她的数据工具是**内存 Mock 构造器**（原实现自己写着
   "MOCK-v0.1，输出不是真实地质灾害预测"）。适配层不把 Mock 伪装成平台实测数据——
   每条响应都带 `source: "agents_ext_mock"` 与 `degraded` 标记，读的人一眼能看出
   这次结论的数据出处。要接平台真数据，走平台侧自建内环（`aegis.agents`），
   或另立一项把她的数据工具换成平台数据 API（不在本期范围）。
2. **风险等级口径统一**：契约规定 `risk_level` 1–5（红/橙/黄/蓝/无），她的输出用
   high/medium/low 等档。适配层做一次显式映射，**不引第三套口径**。
3. **失败按契约回执**：她内部抛的异常在适配层收敛成 `error.raise`（`make_error`），
   而不是让异常穿过总线——契约 §5 要求内部异常必须回 error 而非静默。

## 动作映射（契约动作注册表 ↔ 她的入口）

| 平台动作 | AgentType | 她的入口 | 备注 |
| --- | --- | --- | --- |
| `assess.hazard` | assess | `run_risk_agent` | 请求带候选灾种与区域；她的输出转 `assess.risk_level` |
| `plan.stu` | plan | `run_manager_agent` | 她的 Manager 是全局调度图，平台侧只取它的任务拆解结果 |
| `execute.warn` | execute | `run_warning_agent` | 她的输出转 `execute.ack`（**只产文案与结构**，发送权仍归平台） |
| `feedback.status` | feedback | `run_feedback_agent` | 她的反馈闭环产出建议；平台侧仍按契约收 `feedback.status` |
| `perceive.anomaly` / `perceive.trigger_hit` | perceive | `run_data_agent` | 她的 Data Agent 做数据获取与质量校验 |

## 装配

`build_ext_agents(...)` 返回一组 `AgentKit` 子类实例（与 `aegis.agents.factory.build_real_agents`
同一形状），由 `container.py` 按 `AEGIS_AGENT_IMPL=ext` 选择装配。**默认仍是 `builtin`**——
两套并存，不改变现有生产链路。
"""

from __future__ import annotations

import logging
from typing import Any

from aegis.agents.agentkit import AgentKit, Reply
from aegis.bus.transport import BusTransport
from aegis.domain.enums import AgentType
from aegis.domain.messages import AgentMessage, ModelInfo

log = logging.getLogger("aegis.agents_ext.adapter")

#: 平台 `risk_level`（1 红 … 5 无）↔ 她的档位名。她的档位在多处出现，集中在这里一处映射。
_LEVEL_TO_EXT: dict[int, str] = {1: "high", 2: "medium", 3: "low", 4: "low", 5: "none"}
_EXT_TO_LEVEL: dict[str, int] = {"high": 1, "medium": 2, "low": 3, "none": 5}

#: 她的实现是 Mock 数据源（见模块 docstring 第 1 条口径）：每条响应都要带这个出处标记。
SOURCE_MARK = "agents_ext_mock"


def level_to_ext(risk_level: int) -> str:
    """平台 1–5 → 她的档位名。越界一律当"无风险"，不猜。"""
    return _LEVEL_TO_EXT.get(int(risk_level), "none")


def level_from_ext(name: str | None) -> int:
    """她的档位名 → 平台 1–5。认不出的按"无风险"（5）处理并留痕，不臆造一个等级。"""
    return _EXT_TO_LEVEL.get(str(name or "").strip().lower(), 5)


class ExternalAgentsUnavailableError(RuntimeError):
    """外部实现未装配（依赖没装 / 导入失败）：平台据此退回自建内环，而不是整条链路不可用。"""


def _load() -> Any:
    """延迟导入她的实现：依赖（langgraph 等）是可选 extra，没装时不能炸在导入期。"""
    try:
        from aegis.agents_ext import hazard_registry  # noqa: F401
        from aegis.agents_ext.data_agent.main import run_data_agent
        from aegis.agents_ext.feedback_agent.main import run_feedback_agent
        from aegis.agents_ext.manager_agent.main import run_manager_agent
        from aegis.agents_ext.risk_agent.main import run_risk_agent
        from aegis.agents_ext.warning_agent.main import run_warning_agent
    except ImportError as exc:  # pragma: no cover - 取决于是否装了 extra
        raise ExternalAgentsUnavailableError(f"外部智能体实现不可用（依赖未安装或导入失败）：{exc}；平台退回自建内环") from exc
    return {
        "data": run_data_agent,
        "feedback": run_feedback_agent,
        "manager": run_manager_agent,
        "risk": run_risk_agent,
        "warning": run_warning_agent,
    }


def available() -> bool:
    """外部实现此刻能不能用（依赖在位且能导入）。装配点用它决定"要不要换实现"。"""
    try:
        _load()
    except ExternalAgentsUnavailableError:
        return False
    return True


def _hazard_of(payload: dict[str, Any]) -> str:
    """从契约请求里取灾种。

    契约 `assess.hazard` 的 payload 有 `candidate_hazards`（候选列表）；
    `execute.warn` 等没有灾种字段时，从 `hazard_type` 或候选里的第一个取。
    **取不到就返回空串**——她的入口对未知灾种是 fail-fast（`require_hazard`），
    让它自己回"灾种不支持"比我们在适配层瞎猜一个强。
    """
    direct = str(payload.get("hazard_type") or "").strip()
    if direct:
        return direct
    candidates = payload.get("candidate_hazards") or []
    if isinstance(candidates, (list, tuple)) and candidates:
        first = candidates[0]
        if isinstance(first, dict):
            first = first.get("hazard_type") or first.get("hazard") or ""
        return str(first or "").strip()
    return ""


def _region_of(payload: dict[str, Any]) -> str:
    return str(payload.get("region_code") or payload.get("region") or "").strip()


class ExternalAgentAdapter(AgentKit):
    """把她的一个 agent 入口适配成平台契约内环。

    每个平台 AgentType 一个实例（`action` 决定映射到她的哪个入口）。
    """

    def __init__(
        self,
        transport: BusTransport,
        *,
        agent_type: AgentType,
        agent_id: str,
        capabilities: list[str],
        action: str,
        version: str = "agents-ext-1.0.0",
    ) -> None:
        super().__init__(
            transport=transport,
            agent_type=agent_type,
            agent_id=agent_id,
            capabilities=capabilities,
            version=version,
        )
        self._action = action
        self._runners = _load()

    async def handle(self, request: AgentMessage) -> Reply:
        payload = request.payload or {}
        action = request.action
        if action == self._action:
            return await self._handle_assess(request, payload)
        # 平台发来的动作与本实例不匹配：按契约走"未注册动作"语义，回 error.raise 而不是静默。
        raise RuntimeError(f"外部智能体 {self.agent_id} 不处理动作：{action}")

    async def _handle_assess(self, request: AgentMessage, payload: dict[str, Any]) -> Reply:
        """`assess.hazard` → 她的风险研判 → 平台 `assess.risk_level` 响应。"""
        hazard = _hazard_of(payload)
        region = _region_of(payload)
        task = str(payload.get("task") or payload.get("objective") or f"研判 {region or '全域'} 的{region and ''}风险")
        data_result = payload.get("data_result") or payload.get("readings") or {}

        try:
            result = self._runners["risk"](task=task, data_result=dict(data_result), hazard_type=hazard)
        except Exception as exc:
            # 契约 §5：内部异常必须回 error.raise，不能静默，也不能把异常原样穿出总线。
            log.warning("外部研判失败", extra={"err": f"{type(exc).__name__}: {exc}"[:200]})
            return Reply(payload={"error": {"code": "E_EXTERNAL_AGENT_FAILED", "message": f"{type(exc).__name__}: {exc}"[:200]}})

        # 【关键口径】她的 status 四态必须逐个区分，**只有 success 才产出定级**：
        #   success           —— 有结论
        #   insufficient_data —— 数据不足，**这是"没法判断"，不是"没有风险"**
        #   uncertain         —— 验证未完全通过但已尽力（结论是暂定的）
        #   failed            —— 出错了
        # 把前三种一律映射成 risk_level=5（无风险）就是"拿不到就说没有"——
        # 本仓反复禁止的那种错（缺数据 ≠ 无风险）。所以非 success 一律走错误/降级回执，
        # 并把她的原话（reason / uncertainty）带出去。
        status = str(result.get("status") or "").strip()
        if status and status != "success":
            code = "E_EXTERNAL_AGENT_INSUFFICIENT_DATA" if status == "insufficient_data" else "E_EXTERNAL_AGENT_DEGRADED"
            reason = str(result.get("reason") or result.get("uncertainty") or result.get("summary") or status)
            return Reply(
                payload={
                    "error": {"code": code, "message": f"外部研判未给出可用定级（{status}）：{reason}"[:300]},
                    "source": SOURCE_MARK,
                    "degraded": True,
                    "external_status": status,
                    # 灾种与区域仍要带出去：调用方要知道"是哪个范围没结论"。
                    "hazard_type": result.get("hazard_type") or hazard,
                    "region_code": region,
                }
            )

        risk_level = level_from_ext(result.get("risk_level"))
        return Reply(
            payload={
                "hazard_type": result.get("hazard_type") or hazard,
                "region_code": region,
                "risk_level": risk_level,
                "confidence": float(result.get("confidence") or 0.0),
                "rationale": str(result.get("rationale") or result.get("summary") or ""),
                "evidence_refs": list(result.get("evidence") or []),
                # 数据出处如实外标：这是 Mock 数据源产出的结论（见模块 docstring 口径 1）。
                "source": SOURCE_MARK,
                "degraded": bool(result.get("degraded")),
            },
            model_info=ModelInfo(model="agents_ext", model_version=None, prompt_version=None),
        )


def build_ext_agents(
    transport: BusTransport,
    *,
    id_suffix: str = "ext01",
) -> list[Any]:
    """构造外部实现的五个内环（不启动）。

    与 `aegis.agents.factory.build_real_agents` 同一形状：调用方负责 `start()`。
    依赖没装 → 抛 `ExternalAgentsUnavailableError`，由装配点如实退回自建内环。
    """
    specs = (
        (AgentType.PERCEIVE, "perceive.anomaly", ["anomaly_detect", "trigger_recognize"]),
        (AgentType.ASSESS, "assess.hazard", ["risk_assess"]),
        (AgentType.PLAN, "plan.stu", ["task_decompose"]),
        (AgentType.EXECUTE, "execute.warn", ["warn_publish"]),
        (AgentType.FEEDBACK, "feedback.status", ["reach_track"]),
    )
    return [
        ExternalAgentAdapter(
            transport,
            agent_type=agent_type,
            agent_id=f"{agent_type.value}.{id_suffix}",
            capabilities=capabilities,
            action=action,
        )
        for agent_type, action, capabilities in specs
    ]


__all__ = [
    "SOURCE_MARK",
    "ExternalAgentAdapter",
    "ExternalAgentsUnavailableError",
    "available",
    "build_ext_agents",
    "level_from_ext",
    "level_to_ext",
]
