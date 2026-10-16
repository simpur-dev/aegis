-- 阈值建议台账与人工确认留痕（§3.5 反馈闭环的后半截 / 任务 MB-6）。
--
-- 为什么需要这张表：feedback 智能体早就产出 `threshold_suggestion` + `suggestion_status
-- =pending_human_review`，但那两个键只进过两处——`ops.feedback.status` 是瞬时指标、
-- 段注里的"｜建议 …"是一段字符串。没有任何地方能回答"现在挂着哪几条建议、谁确认过、
-- 改成了多少"，于是 §3.5 的闭环停在"只产不消"，而方案的验收写的是
-- "修正建议→确认→落地的闭环留痕演示"。
--
-- 四条硬约束写在库里而不是写在文档里：
--   1) 状态只有三种；一旦离开 pending，`decided_by` 与 `decided_at` 必须同时有值——
--      空签字人的"已确认"等于没人对这一票负责（与 D9 核签那条同源）；
--   2) `confirmed` 必须带 before/after 与新版本号：**改了阈值却答不出"从多少改到多少"**，
--      审计上就是"有人动过判据，没人说得清动了什么"；
--   3) 效果分缺席就写 NULL，不许用 0 冒充"效果分为 0"（0 分是一个结论，NULL 是一个事实）；
--   4) 一条建议只允许被裁决一次。结论可反复改而历史只剩最后一次，是规则库最坏的形状——
--      要改主意就等下一条新建议（应用侧的二次 UPDATE 匹配 0 行，因此这条由 SQL 条件与
--      `services/threshold_suggestions.py` 一起守住，库里再放一个触发器属于过度设计）。
--
-- 与建议配对的规则行仍然只有一处真源（架构铁律 4）：确认写进 `trigger_rules` 的新版本，
-- 本表只存"这一票为什么这么投"，不复制阈值。
CREATE TABLE IF NOT EXISTS threshold_suggestions (
    suggestion_id text PRIMARY KEY,
    trace_id      text NOT NULL,
    warning_id    text NOT NULL DEFAULT '',
    region_code   text NOT NULL DEFAULT '',
    hazard_type   text NOT NULL DEFAULT '',
    -- 智能体原话与归因依据：这两格是"人为什么要改阈值"的凭据，逐字归档，不做摘要
    suggestion    text NOT NULL CHECK (suggestion <> ''),
    attribution   text NOT NULL DEFAULT '',
    lag_stage     text NOT NULL DEFAULT '',
    effect_score  double precision CHECK (effect_score IS NULL OR (effect_score >= 0 AND effect_score <= 1)),
    status        text NOT NULL DEFAULT 'pending_human_review'
        CHECK (status IN ('pending_human_review', 'confirmed', 'rejected')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    -- 裁决侧：确认时这三格由 `services/threshold_suggestions.py` 在同一事务里写满
    rule_id       text NOT NULL DEFAULT '',
    decided_by    text NOT NULL DEFAULT '',
    decided_at    timestamptz,
    before_value  jsonb,
    after_value   jsonb,
    new_version   integer CHECK (new_version IS NULL OR new_version >= 1),
    decision_note text NOT NULL DEFAULT '',
    CONSTRAINT suggestions_decision_shape CHECK (
        status = 'pending_human_review'
        OR (decided_by <> '' AND decided_at IS NOT NULL)
    ),
    CONSTRAINT suggestions_confirm_shape CHECK (
        status <> 'confirmed'
        OR (rule_id <> '' AND before_value IS NOT NULL AND after_value IS NOT NULL AND new_version IS NOT NULL)
    )
);

COMMENT ON TABLE threshold_suggestions IS
    '阈值建议台账：只追加，一条建议裁决一次；落地版本见 trigger_rules';

CREATE INDEX IF NOT EXISTS threshold_suggestions_status_idx
    ON threshold_suggestions (status, created_at DESC, suggestion_id);
