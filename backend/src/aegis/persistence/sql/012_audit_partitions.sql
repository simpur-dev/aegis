-- P3 第二批：audit_ops 按月 RANGE 分区（保留 24 个月）+ control_actions 双人复核状态列。
-- 依据：设计文档 §7.5、升级方案 §3.1 规则 3（高安全部署可开"双人复核"，默认关闭）。
--
-- 为什么不是一条 ALTER：PostgreSQL 的声明式分区只能在建表时指定，已存在的普通表要换形态，
-- 必须"另建分区父表 → 预建月分区 → 搬数据 → 删旧表"。整份脚本在 _apply_migrations 的
-- 单个事务里跑，中途失败整体回滚，不会留下半套表。
--
-- 四条不变量：
--   1) 审计行一条不丢：旧表先改名保留，搬完并校对序列后才删；
--   2) 「只追加」语义不变：没有任何 UPDATE/DELETE 业务路径；过期分区只 DETACH 成冷归档表，不删行；
--   3) 分区键 ts 必须进主键：分区表上的唯一约束必须包含分区键，故 PK 从 (id) 变成 (id, ts)；
--   4) 幂等靠 schema_migrations 的 checksum 台账（与 001–011 同一机制），不靠脚本自身重放。

-- ---------- 1. 双人复核状态列 ----------
-- 0 = 仅操作者本人确认（默认，D5「指令即执行」）；2 = 两名不同席位各一票。
-- 两列与状态迁移在同库同事务，第二票由一条 UPDATE ... RETURNING 原子完成（见 control_actions.py）。
ALTER TABLE control_actions
    ADD COLUMN IF NOT EXISTS review_required int NOT NULL DEFAULT 0
        CHECK (review_required IN (0, 2)),
    ADD COLUMN IF NOT EXISTS reviewers jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN control_actions.review_required IS
    '0=本人一次确认（默认）；2=两名不同且达到角色门槛的席位复核（AEGIS_ASSISTANT_DUAL_REVIEW_ENABLED）';
COMMENT ON COLUMN control_actions.reviewers IS
    '复核票 [{"operator_id","role","at"}]；第二票与 proposed→executing 迁移在同一条 UPDATE 完成';

-- ---------- 2. audit_ops：旧表让位 ----------
-- 主键索引与 bigserial 序列的名字都跟着旧表名，先改名才能在新建父表时复用同名对象。
ALTER TABLE audit_ops RENAME TO audit_ops_legacy;
ALTER INDEX IF EXISTS audit_ops_pkey RENAME TO audit_ops_legacy_pkey;
ALTER SEQUENCE IF EXISTS audit_ops_id_seq RENAME TO audit_ops_id_seq_legacy;

CREATE TABLE audit_ops (
    id              bigserial,
    action_id       text NOT NULL,
    phase           text NOT NULL CHECK (phase IN ('intent', 'result')),
    operator_id     text NOT NULL,
    role            text NOT NULL,
    action          text NOT NULL,
    space_id        text,
    target          text NOT NULL DEFAULT '',
    before          jsonb,
    after           jsonb,
    utterance       text NOT NULL DEFAULT '',
    resolved_args   jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key text,
    identity_kind   text NOT NULL DEFAULT 'declared',
    ts              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);

COMMENT ON TABLE audit_ops IS
    '动作审计流水：按月 RANGE 分区、只追加；intent 在执行前，result 在执行后，两条共用 action_id';

-- ---------- 3. 分区维护函数（由 PostgresStore.migrate() 调用，不是留着不用的 SQL） ----------
-- months_back 只在 012 首次落地时用一次（把历史行按月份归位）；日常起服只补未来的月分区。
CREATE OR REPLACE FUNCTION aegis_audit_ensure_partitions(months_back int DEFAULT 0, months_ahead int DEFAULT 2)
RETURNS text[] LANGUAGE plpgsql AS $fn$
DECLARE
    base date := date_trunc('month', (now() AT TIME ZONE 'UTC'))::date;
    lo date;
    hi date;
    part text;
    created text[] := ARRAY[]::text[];
    lag int;
BEGIN
    IF months_back < 0 OR months_ahead < 0 THEN
        RAISE EXCEPTION 'audit_ops 分区月数不能为负（months_back=%, months_ahead=%）', months_back, months_ahead;
    END IF;
    FOR lag IN -months_back..months_ahead LOOP
        lo := (base + (lag || ' months')::interval)::date;
        hi := (lo + interval '1 month')::date;
        part := 'audit_ops_' || to_char(lo, 'YYYYMM');
        IF NOT EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = part
        ) THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF audit_ops FOR VALUES FROM (%L) TO (%L)',
                part,
                to_char(lo, 'YYYY-MM-DD') || ' 00:00:00+00',
                to_char(hi, 'YYYY-MM-DD') || ' 00:00:00+00'
            );
            created := array_append(created, part);
        END IF;
    END LOOP;
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname = 'audit_ops_default'
    ) THEN
        CREATE TABLE audit_ops_default PARTITION OF audit_ops DEFAULT;
        created := array_append(created, 'audit_ops_default');
    END IF;
    RETURN created;
END;
$fn$;

-- 超过保留期的月分区**摘除成独立归档表**，不 DROP、不删行：审计「只追加」不因分区维护破例。
-- 归档表改名为 audit_ops_archive_YYYYMM，避免下次 ensure 把同名表误认为仍是分区。
CREATE OR REPLACE FUNCTION aegis_audit_detach_expired(retention_months int DEFAULT 24)
RETURNS text[] LANGUAGE plpgsql AS $fn$
DECLARE
    cutoff text := to_char(
        (date_trunc('month', (now() AT TIME ZONE 'UTC'))::date - (retention_months || ' months')::interval)::date,
        'YYYYMM'
    );
    part text;
    detached text[] := ARRAY[]::text[];
BEGIN
    IF retention_months < 1 THEN
        RAISE EXCEPTION 'audit_ops 保留月数至少 1，收到 %', retention_months;
    END IF;
    FOR part IN
        SELECT c.relname FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE p.relname = 'audit_ops' AND n.nspname = 'public'
          AND c.relispartition AND c.relname ~ '^audit_ops_[0-9]{6}$'
    LOOP
        IF right(part, 6) < cutoff THEN
            EXECUTE format('ALTER TABLE audit_ops DETACH PARTITION %I', part);
            EXECUTE format('ALTER TABLE %I RENAME TO %I', part, 'audit_ops_archive_' || right(part, 6));
            detached := array_append(detached, part);
        END IF;
    END LOOP;
    RETURN detached;
END;
$fn$;

-- 历史行归位：先建好月分区，再 INSERT，超出窗口的落 DEFAULT（冷归档）。
SELECT aegis_audit_ensure_partitions(24, 2);

INSERT INTO audit_ops (
    id, action_id, phase, operator_id, role, action, space_id, target,
    before, after, utterance, resolved_args, idempotency_key, identity_kind, ts
)
SELECT
    id, action_id, phase, operator_id, role, action, space_id, target,
    before, after, utterance, resolved_args, idempotency_key, identity_kind, ts
FROM audit_ops_legacy;

-- 序列从历史最大 id 接着走：不重号，也不留一个"看着能用其实会撞主键"的坑。
SELECT setval('audit_ops_id_seq', COALESCE((SELECT max(id) FROM audit_ops_legacy), 0) + 1, false);

DROP TABLE audit_ops_legacy;

-- 旧表删除后才腾出这两个索引名；建在父表上会由 PG 自动下发到每个分区。
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_ops (ts DESC);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_ops (action_id, phase);
