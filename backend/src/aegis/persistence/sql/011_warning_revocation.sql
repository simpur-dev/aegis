-- 011：预警撤销状态（智能助手 P2 收尾）
--
-- `status` 是 `WarningRecord.status` 的派生观测位，不是独立状态机。撤销只是追加一个时刻：
-- 发布事实（released_at / deliveries / 回执）一律不改写，否则 ≤20min 触达指标会被事后操作污染。
-- 因此这里只加一列并放宽 CHECK，不加"控制状态表"——那会与 released_at 形成第二个真源。
ALTER TABLE warnings ADD COLUMN IF NOT EXISTS revoked_at timestamptz;

ALTER TABLE warnings DROP CONSTRAINT IF EXISTS warnings_status;
ALTER TABLE warnings ADD CONSTRAINT warnings_status CHECK (
    status IN ('draft', 'released', 'delivered', 'partial', 'failed', 'revoked')
);

COMMENT ON COLUMN warnings.status IS 'draft 未发布 / released 已发布待回执 / delivered 全通道回执 / partial 部分回执 / failed 全通道失败 / revoked 已人工撤销';
COMMENT ON COLUMN warnings.revoked_at IS '人工撤销时刻；NULL 表示未撤销。撤销不改 released_at 与触达回执';
CREATE INDEX IF NOT EXISTS ix_warnings_revoked ON warnings (revoked_at) WHERE revoked_at IS NOT NULL;

-- 011（续）：站内通知。与预警广播分开存：告警按 warning_id 去重，通知是人对人的一句话。
CREATE TABLE IF NOT EXISTS assistant_notifications (
    id          bigserial PRIMARY KEY,
    recipient   text NOT NULL,
    message     text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('lead', 'group')),
    sender      text NOT NULL DEFAULT '',
    space_id    text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    read_at     timestamptz
);
CREATE INDEX IF NOT EXISTS ix_notifications_recipient ON assistant_notifications (recipient, created_at DESC);
