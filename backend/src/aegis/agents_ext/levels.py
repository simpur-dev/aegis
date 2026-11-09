"""外部模块的**口径对照**：灾种键、风险等级、状态四态。

## 这一层为什么存在

平台与外部实现对同一件事有两套词汇：

| | 平台 | 外部实现 |
| --- | --- | --- |
| 灾种 | `HazardType`：landslide / rockfall / debris_flow / **avalanche** / **lake_outburst** /
  quake_triggered / unknown | `hazard_registry`：landslide / rockfall / debris_flow /
  **ice_rock_snow_avalanche** / **glacial_lake_outburst** |
| 风险等级 | `RiskLevel` 1–5（红/橙/黄/蓝/无） | `provisional_level`：high / medium / low / unknown（**不产 none**） |
| 结论状态 | （无专门字段，不合契约即回落） | status：success / insufficient_data / uncertain / failed |

没有这张对照表，平台发 `avalanche` 给外部、外部回 `ice_rock_snow_avalanche`，平台侧
`HazardType(...)` 抛 ValueError → 结论被整份丢弃、静默回落规则引擎——
两个灾种（雪崩、冰湖）走外部实现时**永远拿不到自带结论**，而且看不出是哪里不对。

## 三条口径

1. **认不出就不给结论**：档位译不成平台等级时返回 `None`（不是 5）。
   平台的 5 是"无风险"，把"算不出来"写成 5 就是"拿不到就说没有"。
2. **灾种双向映射**：平台 → 外部用于发请求，外部 → 平台用于收结论；平台独有的
   `quake_triggered`/`unknown` 外部不支持（返回 `None`，不硬塞）。
3. **等级不外发**：外部只产 high/medium/low（对应平台 1/2/3），
   平台的蓝(4) 与无(5) 没有外部对应档位 → 返回 `None`，不塞一个会失真的档。
"""

from __future__ import annotations

from typing import Any

#: 结论的数据出处标记：她的数据工具是内存 Mock 源，读的人一眼能看出这次结论的数据出处。
SOURCE_MARK = "agents_ext_mock"

#: 外部灾种键 → 平台 `HazardType` 值。两个键不同的（雪崩、冰湖溃决）在这里对齐。
_EXT_TO_PLATFORM_HAZARD: dict[str, str] = {
    "landslide": "landslide",
    "rockfall": "rockfall",
    "debris_flow": "debris_flow",
    "ice_rock_snow_avalanche": "avalanche",
    "glacial_lake_outburst": "lake_outburst",
}

#: 平台 `HazardType` 值 → 外部灾种键（反向由上面那张表导出，不手写第二份）。
PLATFORM_TO_EXT_HAZARD: dict[str, str] = {platform: ext for ext, platform in _EXT_TO_PLATFORM_HAZARD.items()}

#: 外部档位 → 平台 `RiskLevel`（1 红 … 5 无）。
#: **没有 unknown**：外部的 "unknown" 是"算不出来"，不是"无风险"，所以它不进这张表
#: （`level_from_ext` 对它返回 None → 不产定级）。
_EXT_TO_LEVEL: dict[str, int] = {"high": 1, "medium": 2, "low": 3, "none": 5}

#: 平台 `RiskLevel` → 外部档位。只有 1/2/3 有对应；蓝(4) 与无(5) 外部不产，返回 None。
_LEVEL_TO_EXT: dict[int, str] = {1: "high", 2: "medium", 3: "low"}

#: 她的 status 四态 → 平台侧的错误码（都压到 21 字符以内：`agent_message.v1.schema.json`
#: 的 code pattern 是 `^E[A-Z0-9_]{3,20}$`，超长会被网关以 E_SCHEMA_INVALID 拒收）。
_STATUS_CODES: dict[str, str] = {
    "insufficient_data": "E_EXT_INSUFFICIENT",
    "uncertain": "E_EXT_UNCERTAIN",
    "failed": "E_EXT_FAILED",
}

#: 动作不支持的错误码（同样要 ≤21 字符）。
CODE_ACTION_UNSUPPORTED = "E_EXT_ACTION_UNSUP"


def hazard_from_platform(platform_key: str) -> str | None:
    """平台灾种 → 外部灾种键。平台独有的（quake_triggered/unknown）返回 None。"""
    return PLATFORM_TO_EXT_HAZARD.get(str(platform_key or "").strip())


def hazard_to_platform(ext_key: str) -> str | None:
    """外部灾种键 → 平台 `HazardType` 值。认不出返回 None（**不回落 unknown**）。"""
    return _EXT_TO_PLATFORM_HAZARD.get(str(ext_key or "").strip())


def level_from_ext(name: str | None) -> int | None:
    """外部档位 → 平台 `RiskLevel`。

    **认不出就返回 `None`**（不给定级）：外部的 "unknown" 是"算不出来"，
    平台的 5 是"无风险"——把前者写成后者就是"拿不到就说没有"。
    """
    return _EXT_TO_LEVEL.get(str(name or "").strip().lower())


def level_to_ext(risk_level: int) -> str | None:
    """平台 `RiskLevel` → 外部档位。蓝(4)/无(5) 外部不产，返回 None（不塞会失真的档）。"""
    return _LEVEL_TO_EXT.get(int(risk_level))


def degraded_code(status: str, result: dict[str, Any]) -> tuple[str, str]:
    """她的非 success 状态 → (平台错误码, 原因文本)。

    原因文本优先取她的原话（`reason` / `missing_info` / `uncertainties` / `error_message`），
    没有就用状态名本身——调用方要能看出"是哪个范围、因为什么没结论"，而不是只拿到一个错误码。
    """
    code = _STATUS_CODES.get(status, "E_EXT_DEGRADED")
    reasons = result.get("reason") or result.get("uncertainty") or result.get("summary")
    if not reasons:
        missing = result.get("missing_info")
        if isinstance(missing, list) and missing:
            reasons = "缺失信息：" + "、".join(str(item) for item in missing)
    if not reasons:
        error_message = result.get("error_message")
        reasons = str(error_message) if error_message else status
    return code, f"外部研判未给出可用定级（{status}）：{reasons}"[:300]


__all__ = [
    "CODE_ACTION_UNSUPPORTED",
    "PLATFORM_TO_EXT_HAZARD",
    "SOURCE_MARK",
    "degraded_code",
    "hazard_from_platform",
    "hazard_to_platform",
    "level_from_ext",
    "level_to_ext",
]
