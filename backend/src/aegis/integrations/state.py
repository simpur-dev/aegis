"""状态行 `IntegrationState` 与存储束 `StoreBundle`：每条腿对外要说的那一行事实的形状。（P2 纯搬运：函数体逐字未改。）"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aegis.storage.store import StoreProtocol


@dataclass(frozen=True, slots=True)
class IntegrationState:
    """一条装配事实：是否启用、用什么驱动、以及可观测的降级细节。"""

    name: str
    enabled: bool
    driver: str
    detail: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "enabled": self.enabled, "driver": self.driver, "detail": dict(self.detail)}

    def degradation_reason(self) -> str:
        """这条腿"带着问题在跑"的成因；空串表示没有问题。

        判据是**标记键有没有值**，不是键在不在：`build_retrieval` 健康时也写
        `degraded=""`（"没有降级原因"是这条记录的一部分），按键存在与否判定会让它
        永远出现在降级清单里——这是真跑一次 `/api/v1/integrations` 才暴露出来的。
        """
        marks = [
            str(value)
            for key, value in self.detail.items()
            if (key.startswith("degraded") or key.endswith("_error") or key == "error") and str(value or "").strip()
        ]
        return "; ".join(marks)


@dataclass(slots=True)
class StoreBundle:
    """存储面 + 需要生命周期管理的实现体（内存后端为 None）+ 一条初始事实。"""

    store: StoreProtocol
    state: IntegrationState
    durability: Any | None = None
