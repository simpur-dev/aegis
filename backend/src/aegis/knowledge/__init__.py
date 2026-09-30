"""AEGIS 知识层：山地灾害预案案例库 + Graphiti 时序知识图谱。

对外只暴露一个契约（`KnowledgeProvider`）与两种实现：
    GraphitiKnowledgeProvider  —— 真实时序图谱（语义 + BM25 + 图遍历混合检索）
    InMemoryKnowledgeProvider  —— 确定性关键词打分，图谱缺席时的降级形态
`FallbackKnowledgeProvider` 把两者组装成"图谱优先、平台降级"的读路径，并统一预算与降级记账。

硬约束：`recall()` 永不触发 LLM 调用（Graphiti 的 `add_episode` 每次约 4—7 次模型调用），
只有 `learn()`（案例入库/复盘）才允许写图。装配入口是 `build_knowledge_provider`。
"""

from __future__ import annotations

from aegis.knowledge.cases import (
    DEFAULT_CASES_PATH,
    HazardCase,
    cases_by_id,
    load_builtin_cases,
    load_cases,
)
from aegis.knowledge.graphiti_store import (
    GraphitiConfig,
    GraphitiKnowledgeProvider,
    GraphitiRole,
    GraphitiUnavailableError,
    LlmCallInRecallError,
    case_episode_uuid,
    case_id_from_episode_uuid,
    group_id_for,
    group_ids_for,
    hybrid_search_config,
)
from aegis.knowledge.memory_store import InMemoryKnowledgeProvider
from aegis.knowledge.provider import (
    DEFAULT_RECALL_BUDGET_MS,
    CaseMatch,
    DegradationRecord,
    FallbackKnowledgeProvider,
    KnowledgeConfigError,
    KnowledgeProvider,
    build_knowledge_provider,
)

__all__ = [
    "DEFAULT_CASES_PATH",
    "DEFAULT_RECALL_BUDGET_MS",
    "CaseMatch",
    "DegradationRecord",
    "FallbackKnowledgeProvider",
    "GraphitiConfig",
    "GraphitiKnowledgeProvider",
    "GraphitiRole",
    "GraphitiUnavailableError",
    "HazardCase",
    "InMemoryKnowledgeProvider",
    "KnowledgeConfigError",
    "KnowledgeProvider",
    "LlmCallInRecallError",
    "build_knowledge_provider",
    "case_episode_uuid",
    "case_id_from_episode_uuid",
    "cases_by_id",
    "group_id_for",
    "group_ids_for",
    "hybrid_search_config",
    "load_builtin_cases",
    "load_cases",
]
