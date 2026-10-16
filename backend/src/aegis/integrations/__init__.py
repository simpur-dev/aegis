"""装配层的可选子系统：按配置组装、失败即降级、降级事实对外可见。

平台内核（总线/网关/链路/工作流）不 import 持久层与分析层；这条边界由这个包守住——
只有装配容器经由这里拿到实现体。于是"没装 extra / 连不上后端 / 配置关闭"三种情况
都不改变链路语义，只改变状态行里的一行事实。

降级口径（与"智能体优先·平台降级"同构）：
- 存储：`memory` 只有内存读视图；`postgres` 在同一读视图前挂 PostGIS/pgvector 落库。
  连接失败**不换实现**——读视图照常服务，写侧交给有界缓冲重试并按计数暴露故障。
- 分析：`off` 时链路里根本不出现 OLAP 代码路径；启用时只做 write-behind 入队，
  摄取与研判热路径永不等 OLAP。
- 知识：图谱缺位时召回自动落到内存案例库，预案生成照旧完成；召回失败只记一条降级，
  绝不把异常抛进预警路径。

模块划分就一件事：一条腿一个文件，公共的只有 `state.py`（状态行与存储束）与
`shared.py`（同一个 logger 与连接串脱敏）。另有三个"不是腿、但只能在装配层出现"的翻译件：
`channels.py`（总线与触达通道的实现选择）、`evidence.py`（案例/检索命中 → 解析器的 `EvidenceNote`）、
`sandbox.py`（案例命中与沙盒 → DAG 节点要的调用口）——它们都把可选腿的形状折成内核协议，
所以放在这里，不放回容器（P4 前它们住在 `container.py`，那是容器长到 1175 行的原因之一）。
装配点（`container.py`）与本包的门面
是唯一入口；`build_*` 之外的 `_mcp_*_source` 是 mcp 工具的私有实现，测试直连它们
是为了看"发出去的那个调用长什么样"（见 `tests/unit/test_mcp_tool_sources.py`）。
"""

from aegis.integrations.analytics import AnalyticsRecorder, build_analytics, chain_facts, reading_facts
from aegis.integrations.channels import build_delivery_client, build_transport, default_channels
from aegis.integrations.evidence import AssembledEvidence
from aegis.integrations.knowledge import build_knowledge, close_knowledge, warm_knowledge
from aegis.integrations.mcp import McpAssembly, build_mcp
from aegis.integrations.memory import build_memory
from aegis.integrations.mqtt import build_mqtt
from aegis.integrations.retrieval import build_retrieval, warm_retrieval
from aegis.integrations.sandbox import AssembledScenarios, sandbox_runner
from aegis.integrations.shared import target_of
from aegis.integrations.state import IntegrationState, StoreBundle
from aegis.integrations.store import build_store, start_store, stop_store
from aegis.integrations.weather import build_weather

__all__ = [
    "AnalyticsRecorder",
    "AssembledEvidence",
    "AssembledScenarios",
    "IntegrationState",
    "McpAssembly",
    "StoreBundle",
    "build_analytics",
    "build_delivery_client",
    "build_knowledge",
    "build_mcp",
    "build_memory",
    "build_mqtt",
    "build_retrieval",
    "build_store",
    "build_transport",
    "build_weather",
    "chain_facts",
    "close_knowledge",
    "default_channels",
    "reading_facts",
    "sandbox_runner",
    "start_store",
    "stop_store",
    "target_of",
    "warm_knowledge",
    "warm_retrieval",
]
