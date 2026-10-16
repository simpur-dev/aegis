"""execute 执行智能体内环（§3.4）：只产文案，不碰发送——发送权属于平台交付腿。

角色分工（论文 §3.2.4 的受众画像三分支）：决策领导（要津 + 依据链 + 责任面）、救援队
（处置动作 + 时限 + 装备）、民众（白话行动指令 + 避险路线 + 求助方式）。藏汉双语的藏文经
平台 `translate` 腿产出，缺位时如实标 `translation_pending`，不假装已经翻译。

两条不许越过的线：
- **数字必须有出处**：每条分支的数字化逐一比对 `content_refs` 与平台既有文案给出的事实池
  （§3.4 防幻觉闸，与 assistant 同款 `text_guard`）；含无出处数字的分支整条弃用并记进
  `rejected_roles`，绝不"润色"成看起来安全的版本。
- **不自称送达**：本体内环的输出里没有 `channel_results`，也不写 `released_at`——链路的
  触达证据只认 `DeliveryDispatcher` 的真回执（§3.4/铁律 7，见 `pipeline/stages.py` 的 `execute`）。

受众文案的落库口径：`WarningRecord.audience_copies`（D10 只增的可选字段，`006_warning_audience_copies.sql`
加列）承载三分支；链路只采纳【过了数字闸】的分支（`pipeline/stages.py` 的 `_adopt_agent_copy` 逐条再验一遍——
它是信任边界，不假定智能体已经检过）。`body_zh` 取民众分支作为公共触达面正文，另两支只进
`audience_copies`，不改公共正文。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from aegis.agents.agentkit import AgentKit, Reply
from aegis.domain.messages import AgentMessage, ModelInfo
from aegis.services.interpret.text_guard import invented_numbers

ROLES = ("decision_lead", "responder", "residents")
ROLE_CN = {"decision_lead": "决策领导", "responder": "救援队", "residents": "民众"}


class ExecuteBranches(BaseModel):
    """结构化输出的目标 schema：三分支文案 + 标题，字段冻结、不许多给。"""

    model_config = {"extra": "forbid"}

    title_zh: str = ""
    branches: dict[str, str] = Field(default_factory=dict)


@dataclass
class ExecuteAgent(AgentKit):
    roles: tuple[str, ...] = ROLES
    #: 采纳进预警正文的分支角色（公共触达面）；其余分支只随响应回传，当前不入库（见模块 docstring）。
    body_role: str = "residents"

    async def handle(self, request: AgentMessage) -> Reply:
        payload = request.payload or {}
        raw_content = payload.get("content")
        content: dict[str, Any] = raw_content if isinstance(raw_content, dict) else {}
        base_zh = str(content.get("zh") or "")
        base_bo = content.get("bo")
        # 事实池 = 链路交下来的 content_refs + 平台既有文案 + 本次预警的区域与等级。
        # 区域码/等级必须在池里：否则文案正常写"540121"也会被防幻觉闸误杀（假阳性比漏检更劝退）。
        scope = " ".join([*(str(item) for item in (payload.get("regions") or [])), str(payload.get("level", ""))])
        facts = f"{payload.get('content_refs') or ''} {base_zh} {base_bo or ''} {scope}".strip()

        if self.llm is None:
            # 没有网关就没有生成本体：原样把平台文案回回去，is_llm=False 让链路知道这是回落轨。
            return Reply(
                payload={
                    "warning_id": payload.get("warning_id", ""),
                    "title_zh": "",
                    "body_zh": base_zh,
                    "body_bo": base_bo,
                    "audience_copies": {},
                    "rejected_roles": [],
                    "translation_pending": base_bo in (None, ""),
                    "degraded": "未配置 LLM 网关：文案回落平台模板，三分支与翻译均不声称",
                }
            )

        variables = {
            "level": payload.get("level", ""),
            "regions": "、".join(str(item) for item in (payload.get("regions") or [])),
            "audiences": "、".join(str(item) for item in (payload.get("audiences") or [])),
            "facts": facts,
            "content": base_zh,
        }
        result = await self.llm.chat_structured(
            [],
            schema=ExecuteBranches,
            task="execute",
            prompt_key="execute",
            prompt_vars=variables,
            agent_id=self.agent_id,
            trace_id=request.trace_id,
        )
        branches, rejected = self._gate(result.branches, facts)
        body_bo, translation_pending = await self._translate(branches.get(self.body_role, base_zh), base_bo, request.trace_id)
        # D10 出处凭证：这一段文字是民众实际读到的，最该能被追问是谁写的。
        # 链路只在文案真进了预警时转记它（`pipeline/stages.py` 的 `execute`），所以这里只管如实报自己用了什么模型。
        return Reply(
            payload={
                "warning_id": payload.get("warning_id", ""),
                "title_zh": result.title_zh.strip(),
                "body_zh": branches.get(self.body_role, base_zh),
                "body_bo": body_bo,
                "audience_copies": branches,
                "rejected_roles": rejected,
                "translation_pending": translation_pending,
                "used_roles": sorted(branches),
            },
            model_info=self._provenance(),
        )

    def _provenance(self) -> ModelInfo | None:
        """与 assess/plan/feedback 同一句口径：凭证来自网关的按任务路由，平台不代填。"""
        getter = getattr(self.llm, "model_info_for_task", None)
        if getter is None:
            return None
        info = getter("execute")
        return ModelInfo(model=info.get("model"), prompt_version=info.get("prompt_version"))

    def _gate(self, raw: dict[str, str], facts: str) -> tuple[dict[str, str], list[str]]:
        """逐分支过防幻觉闸：出现事实之外的数字就整条丢掉，并记下是谁被丢（不静默改写）。"""
        kept: dict[str, str] = {}
        rejected: list[str] = []
        for role in self.roles:
            text = str(raw.get(role) or "").strip()
            if not text:
                continue
            invented = sorted(invented_numbers(text, facts))
            if invented:
                rejected.append(role)
                continue
            kept[role] = text
        return kept, rejected

    async def _translate(self, source: str, existing: Any, trace_id: str | None = None) -> tuple[str | None, bool]:
        """藏文腿：已有译文就沿用；否则请网关翻译，翻不出来就如实标 pending（不空串冒充）。"""
        if isinstance(existing, str) and existing.strip():
            return existing, False
        if not source.strip() or self.llm is None:
            return None, True
        gateway = self.llm
        try:
            translated = await gateway.translate(source, agent_id=self.agent_id, trace_id=trace_id)
        except Exception:
            return None, True
        text = str(translated or "").strip()
        return (text, False) if text else (None, True)
