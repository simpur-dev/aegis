"""领域枚举：灾种、风险等级、智能体类型、动作注册表、交付通道。口径与契约一致。"""

from __future__ import annotations

from enum import IntEnum, StrEnum

HAZARD_CN: dict[str, str] = {
    "landslide": "滑坡",
    "rockfall": "崩塌危岩",
    "debris_flow": "泥石流",
    "avalanche": "冰雪雪崩",
    "lake_outburst": "冰湖溃决",
    "quake_triggered": "震入灾害链",
    "unknown": "未定灾种",
}


class HazardType(StrEnum):
    LANDSLIDE = "landslide"
    ROCKFALL = "rockfall"
    DEBRIS_FLOW = "debris_flow"
    AVALANCHE = "avalanche"
    LAKE_OUTBURST = "lake_outburst"
    QUAKE_TRIGGERED = "quake_triggered"
    UNKNOWN = "unknown"

    @property
    def cn(self) -> str:
        return HAZARD_CN[self.value]


class RiskLevel(IntEnum):
    """统一风险等级口径：1 最高（红）。所有智能体与平台共用，禁止自定义分级。"""

    RED = 1
    ORANGE = 2
    YELLOW = 3
    BLUE = 4
    NONE = 5

    @property
    def cn(self) -> str:
        return {1: "红色", 2: "橙色", 3: "黄色", 4: "蓝色", 5: "无风险"}[int(self)]


class AgentType(StrEnum):
    PERCEIVE = "perceive"
    ASSESS = "assess"
    PLAN = "plan"
    EXECUTE = "execute"
    FEEDBACK = "feedback"


class MessageKind(StrEnum):
    REQUEST = "request"
    RESPONSE = "response"
    EVENT = "event"
    ERROR = "error"


class Action(StrEnum):
    # 感知
    PERCEIVE_ANOMALY = "perceive.anomaly"
    PERCEIVE_TRIGGER_HIT = "perceive.trigger_hit"
    # 研判
    ASSESS_HAZARD = "assess.hazard"
    ASSESS_RISK_LEVEL = "assess.risk_level"
    # 决策
    PLAN_STU = "plan.stu"
    PLAN_STU_RESULT = "plan.stu_result"
    PLAN_RESCHEDULE = "plan.reschedule"
    # 执行
    EXECUTE_WARN = "execute.warn"
    EXECUTE_ACK = "execute.ack"
    # 反馈
    FEEDBACK_STATUS = "feedback.status"
    # 框架
    ERROR_RAISE = "error.raise"
    AGENT_REGISTER = "agent.register"
    AGENT_HEARTBEAT = "agent.heartbeat"
    # 数据面（平台 → 总线，非智能体动作）
    TELEMETRY_READING = "telemetry.reading"


REGISTERED_ACTIONS: frozenset[str] = frozenset(a.value for a in Action) - {
    Action.AGENT_REGISTER.value,
    Action.AGENT_HEARTBEAT.value,
}


class TaskType(StrEnum):
    MONITOR = "monitor"
    ASSESS = "assess"
    DECIDE = "decide"
    WARN = "warn"
    DISPATCH = "dispatch"
    EVACUATE = "evacuate"
    VERIFY = "verify"
    REPORT = "report"


class OwnerRole(StrEnum):
    AGENT = "agent"
    HUMAN_COMMANDER = "human_commander"
    MONITOR_OFFICER = "monitor_officer"
    FIELD_INSPECTOR = "field_inspector"
    BROADCASTER = "broadcaster"
    PLATFORM = "platform"


class FallbackMode(StrEnum):
    RETRY = "retry"
    TRANSFER = "transfer"
    ESCALATE = "escalate"
    DEGRADE_TO_RULE = "degrade_to_rule"
    SKIP = "skip"


class Channel(StrEnum):
    SMS = "sms"
    BEIDOU = "beidou"
    BROADCAST = "broadcast"
    WECHAT = "wechat"
    WEBHOOK = "webhook"


class RefType(StrEnum):
    TELEMETRY = "telemetry"
    ENTITY = "entity"
    CASE = "case"
    WARNING = "warning"
    TASK_UNIT = "task_unit"
    OBJECT = "object"
    SNAPSHOT = "snapshot"
