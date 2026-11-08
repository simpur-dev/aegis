-- P4：按人令牌（强认证）+ 通知投递状态列。依据：设计文档 §14.1、§14.2。
--
-- 为什么是这三张/这几列：
--   1) `operator_keys` 把"席位"从声明身份升级为可验证凭据：只存 SHA-256 摘要，明文不落库
--      （库被读走也拿不到可用凭据；摘要 UNIQUE 让"一把令牌对一个席位"是库级约束，不是应用层的自觉）；
--   2) `operator_key_attempts` 把连续失败计数**按人**存而不是按哈希存：哈希对不上时没有身份可归属，
--      按哈希计数等于让攻击者换一把随机令牌就把计数清零；
--   3) `assistant_notifications` 加三列投递状态：站内先行、外发失败不装送达——"发没发出去"必须是
--      可查的事实，而不是接口响应里的一句断言。
--
-- 三条不变量：
--   1) 令牌摘要不进任何读接口（`operator_keys` 的读面只给 key_id/时间戳/是否启用）；
--   2) 失败计数与锁定在**一条语句**里完成（UPSERT + CASE），并发下不会漏计或重复锁定；
--   3) 锁定期内已锁定的 `locked_until` 不被后续失败延长——否则每次失败都把锁定往后推，
--      一次爆破就能让人被永久锁在外面。
--
-- 回滚：DROP TABLE operator_key_attempts → operator_keys；通知表的四列用 ALTER TABLE ... DROP COLUMN 撤。

CREATE TABLE IF NOT EXISTS operator_keys (
    key_id       text PRIMARY KEY CHECK (key_id ~ '^ok_[0-9a-f]{12,24}$'),
    operator_id  text NOT NULL REFERENCES operators(operator_id) ON DELETE CASCADE,
    -- SHA-256 摘要（64 位小写 hex）。明文只在签发响应里出现一次，任何读路径都不返回它。
    key_hash     text NOT NULL UNIQUE CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    active       boolean NOT NULL DEFAULT true,
    last_used_at timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE operator_keys IS
    '按人令牌（强认证凭据）：只存 SHA-256 摘要，明文不落库；吊销走 active=false，不删行（审计要追得到）';
COMMENT ON COLUMN operator_keys.key_hash IS
    'SHA-256(明文令牌) 的小写 hex；不返回给任何读接口——离线爆破摘要也换不回明文，但没必要把靶子递出去';

CREATE INDEX IF NOT EXISTS ix_operator_keys_operator ON operator_keys (operator_id, created_at DESC);

-- 连续失败计数与锁定：按人（operator_id）一行，锁定到期自动失效（不需要定时任务来解）。
CREATE TABLE IF NOT EXISTS operator_key_attempts (
    operator_id    text PRIMARY KEY REFERENCES operators(operator_id) ON DELETE CASCADE,
    failed_attempts int NOT NULL DEFAULT 0 CHECK (failed_attempts >= 0),
    locked_until   timestamptz,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE operator_key_attempts IS
    '按人的令牌认证失败计数与锁定窗口；锁定期内一律拒绝（正确令牌也拒，这是锁定的目的）';

-- 通知投递状态：站内先行、外发失败不装送达。
-- delivery_status 三值：in_app（仅站内，默认）/ delivered（外发成功）/ failed（外发失败）。
ALTER TABLE assistant_notifications
    ADD COLUMN IF NOT EXISTS delivery_channel text NOT NULL DEFAULT 'in_app',
    ADD COLUMN IF NOT EXISTS delivery_status text NOT NULL DEFAULT 'in_app'
        CHECK (delivery_status IN ('in_app', 'delivered', 'failed')),
    ADD COLUMN IF NOT EXISTS delivery_error text,
    ADD COLUMN IF NOT EXISTS delivered_at timestamptz;

COMMENT ON COLUMN assistant_notifications.delivery_status IS
    'in_app=只在站内送达（默认）/ delivered=外部通道也送达 / failed=外部通道失败（站内那一条仍然成立）';
COMMENT ON COLUMN assistant_notifications.delivery_error IS
    '外发失败的原因（供人排查）；不重试是本期有意为之，重试要退避与幂等键，另立一项';
COMMENT ON COLUMN assistant_notifications.delivered_at IS
    '站内送达时刻（created_at 即站内送达）；不因外发失败而清空——站内是真源，外发只是加速器';
