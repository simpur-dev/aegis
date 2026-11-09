"""外部模块的**契约门面**：契约语义 ↔ 外部语义的翻译都在这一层。

## 这一层解决什么

`adapter.py` 实现 `AgentKit.handle`（契约壳的样板：订阅、心跳、幂等、超时、错误回执）；
"两套语义怎么对"全在这一层：

```
AgentKit.handle(request)          ← adapter.py：契约壳样板 + 动作分派
        │
        ▼
contract_face.assess_face(...)    ← contract_face.py：语义翻译（可单独单测）
        │
        ▼
run_risk_agent(...)              ← 她的实现（业务逻辑一个字没动）
```

## 三条硬口径

1. **失败走 error 通道，不塞进 response**：外部失败/数据不足/内部异常一律抛
   `ExternalAgentError`，由 `AgentKit` 转成 `kind=error` 的消息——网关据此记
   `outcome=error`，平台侧走规则降级轨。若把错误塞进 response 的嵌套 dict，
   网关会**当成功记账**，协同成功率指标虚高（这是评审抓到的真问题）。
2. **"没法判断"不变成"没有风险"**：外部的 status 四态逐个区分；认不出的档位
   **不给定级**（`levels.level_from_ext` 返回 None），而不是塞一个"无风险"。
3. **数据出处如实外标**：她的数据工具是内存 Mock 源，结论带 `source=agents_ext_mock`。

## 已知的数据面缺口（如实记在这里，不假装已接通）

平台的研判请求给的是**规则命中**（`hits`：rule_id / hazard_type / score / observed_at），
外部因子引擎要的是**观测事件**（`processed_data.events`：含 rainfall_mm_h 等灾种因子字段）。
两者不是一回事：本层把 `hits` 转成事件骨架喂过去，但**因子字段缺失**，
外部因子引擎会按它的门控判 `insufficient_data` —— 这是诚实降级，不是接通。

要让外部研判真正产出结论，需要平台在研判请求里带上原始观测
（外部实现经平台数据 API 取数，见接入规范 §4）。本期不做，记在 ADR-0011 的"已知限制"里。
"""

from __future__ import annotations

import logging
from typing import Any

from aegis.agents_ext.levels import (
    CODE_HAZARD_UNSUPPORTED,
    CODE_LEVEL_UNKNOWN,
    CODE_NO_CONFIDENCE,
    SOURCE_MARK,
    degraded_code,
    hazard_from_platform,
    hazard_to_platform,
    level_from_ext,
)

log = logging.getLogger("aegis.agents_ext.contract_face")

FIELD_REGION = "region_code"
FIELD_HAZARD = "hazard_type"
FIELD_CANDIDATES = "candidate_hazards"
FIELD_DATA = "data_result"
FIELD_HITS = "hits"


class ExternalAgentError(RuntimeError):
    """外部实现给不出可用结论。

    `code` 是平台错误码（≤21 字符，过 `agent_message.v1.schema.json` 的 pattern）；
    `retryable` 说明这类失败重试有没有意义。

    属性名与 `AgentKit` 的读取口径一致（`error_code` / `error_detail` / `retryable`）：
    壳靠这三个属性把失败转成 `kind=error` 的消息——**只有走 error 通道**，
    网关才会记 `outcome=error`、平台侧才走规则降级轨。塞进 response 的嵌套 dict
    会被网关**当成功记账**（评审抓到的真问题：协同成功率指标虚高）。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = code
        self.message = message
        self.retryable = retryable
        self.error_detail = detail or {}


def hazard_candidates(payload: dict[str, Any]) -> list[str]:
    """契约请求里的候选灾种（`candidate_hazards` 列表，或单个 `hazard_type`）。"""
    raw = payload.get(FIELD_CANDIDATES)
    candidates: list[str] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                item = item.get(FIELD_HAZARD) or item.get("hazard") or ""
            text = str(item or "").strip()
            if text:
                candidates.append(text)
    direct = str(payload.get(FIELD_HAZARD) or "").strip()
    if direct and direct not in candidates:
        candidates.insert(0, direct)
    return candidates


def pick_hazard(payload: dict[str, Any]) -> str:
    """候选灾种 → 外部灾种键（**第一个外部支持的那个**）。

    平台独有灾种（quake_triggered / unknown）与外部键名不同的（avalanche ↔
    ice_rock_snow_avalanche）都在 `levels` 的对照表里转换；全都映射不了时返回空串，
    由调用方按"灾种不支持"拒绝——**不硬塞一个近似灾种**（她的模块自己写着"绝不擅自猜测、
    替换成相近灾种"，平台侧也得守同一条）。
    """
    for candidate in hazard_candidates(payload):
        mapped = hazard_from_platform(candidate)
        if mapped:
            return mapped
    return ""


def region_of(payload: dict[str, Any]) -> str:
    return str(payload.get(FIELD_REGION) or payload.get("region") or "").strip()


def events_for(payload: dict[str, Any]) -> dict[str, Any]:
    """把平台请求里的观测数据整理成外部要的形状。

    两种输入：
    - `data_result`：已经是上游 Data 的结果（外部形状，`processed_data.events`）；
    - `hits`：平台研判请求带的**规则命中**（TriggerHit 字典列表）——转成事件骨架。

    **注意**：命中不是观测。转出来的事件缺 `rainfall_mm_h` 这类灾种因子字段，
    外部因子引擎会按门控判数据不足——这是诚实降级，不是接通（见模块头"已知的数据面缺口"）。
    """
    provided = payload.get(FIELD_DATA)
    if isinstance(provided, dict) and provided:
        return provided
    hits = payload.get(FIELD_HITS)
    events: list[dict[str, Any]] = []
    if isinstance(hits, (list, tuple)):
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            events.append(
                {
                    "event_id": str(hit.get("rule_id") or hit.get("event_id") or ""),
                    "time": str(hit.get("observed_at") or ""),
                    "region": str(hit.get("region_code") or ""),
                    # 命中分数原样带过去（外部按 factor 字段取数，命中分数不是因子）
                    "hit_score": hit.get("score"),
                }
            )
    return {
        "processed_data": {"events": events, "data_source": "platform_hits", "is_mock": False},
        "data_quality": {"is_valid": bool(events)},
        "missing_data": [] if events else ["平台研判请求未带观测事件（hits 是规则命中，不是观测）"],
        "status": "success" if events else "insufficient_data",
    }


def task_of(payload: dict[str, Any], region: str) -> str:
    """她的入口要一句自然语言任务描述；从契约 payload 里取，取不到就据区域现编一句。

    注意：这句任务串只是**给她的规则引擎读的上下文**，不是平台下发的指令——
    平台的指令原文在审计的 `utterance` 列里（`AgentMessage` 本身没有这个字段），两处不混。
    """
    explicit = str(payload.get("task") or payload.get("objective") or "").strip()
    if explicit:
        return explicit
    return f"研判 {region or '全域'} 的风险" if region else "研判全域风险"


def assess_face(request_payload: dict[str, Any], runner: Any, *, agent_id: str = "assess.agents_ext") -> dict[str, Any]:
    """`assess.hazard` → 她的风险研判 → 平台契约响应载荷。

    **给不出可用结论就抛 `ExternalAgentError`**（而不是返回一个带 `error` 字段的响应）：
    只有走 error 通道，网关才会记 `outcome=error`，平台侧才会走规则降级轨。
    """
    hazard = pick_hazard(request_payload)
    if not hazard:
        raise ExternalAgentError(
            CODE_HAZARD_UNSUPPORTED,
            f"外部实现不支持该灾种：{hazard_candidates(request_payload) or '（未给灾种）'}",
        )
    region = region_of(request_payload)
    task = task_of(request_payload, region)
    data_result = events_for(request_payload)

    try:
        result = runner(task=task, data_result=data_result, hazard_type=hazard)
    except Exception as exc:
        # 内部异常：记一条 warning（原来完全静默，外部崩溃在平台侧看不出任何痕迹）
        log.warning("外部研判异常", extra={"err": f"{type(exc).__name__}: {exc}"[:200], "hazard_type": hazard})
        raise ExternalAgentError(
            "E_EXT_FAILED",
            f"外部研判异常：{type(exc).__name__}: {exc}"[:200],
            retryable=True,
        ) from exc

    status = str(result.get("status") or "").strip()
    if status != "success":
        code, reason = degraded_code(status, result)
        log.info("外部研判未给出结论", extra={"status": status, "hazard_type": hazard})
        raise ExternalAgentError(code, reason, detail={"external_status": status, "hazard_type": hazard})

    level = level_from_ext(result.get("risk_level"))
    if level is None:
        # 档位认不出 = "算不出来"：**不产定级**（平台的 5 是"无风险"，不是这里的意思）
        raise ExternalAgentError(
            CODE_LEVEL_UNKNOWN,
            f"外部研判档位无法映射到平台等级：{result.get('risk_level')!r}",
            detail={"hazard_type": hazard},
        )

    # 契约要 confidence（平台会校验 0..1），外部**没有这个字段**——
    # 拿不到真值就不产结论（填 0.0 会被平台当成一条有效研判采纳）。
    raw_confidence = result.get("confidence")
    if raw_confidence is None:
        raise ExternalAgentError(
            CODE_NO_CONFIDENCE,
            "外部研判未给出置信度（契约要求 confidence），本次不产结论",
            detail={"hazard_type": hazard},
        )
    confidence = float(raw_confidence)

    platform_hazard = hazard_to_platform(str(result.get("hazard_type") or hazard)) or hazard_from_platform(hazard) or ""
    return {
        "hazard_type": platform_hazard,
        "region_code": region,
        "risk_level": level,
        "confidence": confidence,
        "rationale": str(result.get("rationale") or result.get("reasoning") or result.get("summary") or "")[:1000],
        # 契约里 evidence_refs 是**引用列表（字符串）**；外部给的是 dict（三元组），这里取其 id/note 拼成串
        "evidence_refs": [
            item if isinstance(item, str) else str(item.get("note") or item.get("source") or item.get("type") or "")
            for item in (result.get("evidence") or [])
        ],
        # 结论主体：**实例 id**（与自建内环同一口径，见 agents/assess.py 的 `self.agent_id`）——
        # 台账要能分清是哪台本体答的，不只是"外部 vs 自建"。
        "assessed_by": agent_id,
        # 低置信人工核签护栏（§3.5）：外部实现自报"是否建议转人工"；没有就 False（= 不建议）
        "human_review_suggested": bool(result.get("human_review_suggested") or False),
        # 双盲增强腿：外部实现没有这条腿，显式写"不适用"而不是留空（空会被读成"没评估"）
        "double_blind": "not_applicable",
        "double_blind_mode": "",
        # 数据出处：外部结论出自内存 Mock 源，读的人一眼能看出
        "source": SOURCE_MARK,
    }


__all__ = [
    "ExternalAgentError",
    "assess_face",
    "events_for",
    "hazard_candidates",
    "pick_hazard",
    "region_of",
    "task_of",
]
