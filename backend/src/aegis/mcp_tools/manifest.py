"""MCP 工具清单（§4.2 / D4）：纯数据，不 import `mcp`，因此未装可选腿时也能被读与被测。

为什么清单是单一真源：`server.py` 要靠函数签名生成 inputSchema，参数表如果另立一张，就会漂成
"文档里有这个参数、schema 里没有"。所以【名称/摘要/只读性/落在哪个注入端口/参数表/默认值】全在这一条记录里，
装配与用例都从它出发。

`source` 记的是"这个工具复用平台的哪条既有能力"（写到端点/端口级别）。它存在的理由是纪律性的：
MCP 层不许长业务逻辑，新写一份取数口径就是再造一套与 API/链路不同源的真相。参数表因此必须
与被复用那条能力的真实签名一致——对不上就是工具在承诺能力没有的东西。

清单里没有 `graph.search`：`knowledge` provider 只有 `recall`/`learn` 这一对读写面，图谱与内置预案
走的是【同一条 recall】（provider 内部再分主/备），没有独立的"图谱只读面"可暴露。以前想过给它单列一行
（方案 §4.2 表里那行 `graph.search`），但那会让外部客户端以为存在"绕过案例库的图谱查询"——
宁可不做，也不做出一个名不副实的工具。等图谱侧真长出独立读面再加。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    summary: str
    read_only: bool
    #: 被复用的既有能力（端口/方法级），不是"看起来相关"的接口名
    source: str
    #: ToolSources 上对应的那个字段名（注册时按它取实现；取不到就不注册并记为缺席）
    source_attr: str
    #: 对外 inputSchema 的参数表（名称→类型），顺序即 schema 顺序
    params: dict[str, type] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    #: 默认是否对外暴露。False 的仍在清单里（可被显式点名），但不默认给外部客户端看见。
    default_exposed: bool = True


TOOL_MANIFEST: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="telemetry.query",
        summary="按站点/指标/区划/起始时刻取遥测读数（含劣化读数缺口标记）",
        read_only=True,
        source="store.telemetry.query（与 GET /api/v1/telemetry 同一读路径）",
        source_attr="telemetry",
        params={"region_code": str, "station_id": str, "metric": str, "since": str, "limit": int},
        defaults={"limit": 200},
    ),
    ToolSpec(
        name="retrieval.search",
        summary="混合检索：语义佐证片段与各腿分数/出处（缺权重时驱动与 degraded 会如实标出）",
        read_only=True,
        source="retrieval.retrieve(RetrievalQuery)（与 GET /api/v1/retrieval/search 同一条查询）",
        source_attr="retrieval",
        params={"query": str, "k": int, "rerank": bool},
        defaults={"k": 8},
    ),
    ToolSpec(
        name="cases.search",
        summary="案例召回：历史同域预案与出处；图谱缺席时由内置预案兜底并标注 degraded",
        read_only=True,
        source="knowledge.recall（与 GET /api/v1/cases/recall 同一个 provider、同一个预算）",
        source_attr="cases",
        params={"query": str, "region_code": str, "hazard_type": str, "limit": int},
        defaults={"limit": 3},
    ),
    ToolSpec(
        name="memory.search",
        summary="共享记忆命名空间读侧：同区域历史研判/分歧/教训（§4.3 群体认知）",
        read_only=True,
        source="MemoryService.read_shared（与 GET /api/v1/memory/shared 同一门面）",
        source_attr="memory_search",
        params={"kind": str, "region_code": str, "agent_id": str, "limit": int},
        defaults={"limit": 5},
    ),
    ToolSpec(
        name="memory.categories",
        summary="§4.3 四类记忆门面：一次查询同时看见每类『接没接、读到什么、为什么是空』（三类各回独立状态）",
        read_only=True,
        source="MemoryService.read_categories（与 GET /api/v1/memory/categories 同一个门面）",
        source_attr="memory_categories",
        params={"query": str, "region_code": str, "hazard_type": str, "limit": int},
        defaults={"limit": 3},
    ),
    ToolSpec(
        name="memory.write",
        summary="共享记忆写侧：落一条研判/教训进 agent_memory（record_id 与 valid_at 由服务端生成）",
        read_only=False,
        source="MemoryService.write_shared",
        source_attr="memory_write",
        params={"kind": str, "content": str, "agent_id": str, "region_code": str, "disagreement": bool},
        default_exposed=False,  # 写工具：另需 mcp_allow_write_tools 放行，两把钥匙
    ),
    ToolSpec(
        name="simulation.run",
        summary="态势推演沙盒：K 情景 × T 轮的次生灾害概率矩阵与预案对比（确定性数值模型，LLM 不发明数字）",
        read_only=True,
        source="services/simulation.py 的 Sandbox.run（与 POST /api/v1/simulation/run 同一实现）",
        source_attr="simulation",
        params={
            "scenarios": int,
            "rounds": int,
            "rain_base_mm": float,
            "playbook": str,
            "token_budget": int,
            "seed": int,
        },
        defaults={"scenarios": 4, "rounds": 3, "token_budget": 20_000},
        # 默认不暴露：单次是 K×T×角色 的量级，成本由调用方决定；给默认面等于把敞口开给外部客户端。
        default_exposed=False,
    ),
)


def spec_for(name: str) -> ToolSpec | None:
    return next((item for item in TOOL_MANIFEST if item.name == name), None)


def tool_names() -> list[str]:
    return [item.name for item in TOOL_MANIFEST]


def exposed_names(requested: list[str] | None = None, *, allow_write: bool = False) -> list[str]:
    """这一趟到底暴露哪些工具：显式点名优先，否则取清单默认集。

    写工具是【两把钥匙】：既要在 `requested` 里点名，又要 `allow_write=True`——只开闸不把写面送出去，
    只点名不开闸也不行（§4.8.2"工具默认只读"那一层闸）。
    点名点了清单里没有的名字 → ValueError：宁可装配期失败，也不要"配了没生效"（本仓的空旋钮教训）。
    """
    known = {item.name for item in TOOL_MANIFEST}
    if requested:
        unknown = [name for name in requested if name not in known]
        if unknown:
            raise ValueError(f"mcp_tools 里有清单外的工具名 {unknown}（清单共 {len(known)} 项：{sorted(known)}）")
        chosen = list(requested)
    else:
        chosen = [item.name for item in TOOL_MANIFEST if item.default_exposed]
    if not allow_write:
        chosen = [name for name in chosen if (spec := spec_for(name)) is not None and spec.read_only]
    return chosen
