-- 智能助手控制面：席位、动作执行、告警、处置空间/任务、操作审计（设计文档 §7.1）。
--
-- 为什么需要这一组表，而不是继续放在进程内：
--   1) 现有 `PendingAction` / `AssistantSession` 都在内存里，重启即失；"指令即执行"后，
--      一条待确认的发布请求不能因为进程重启就消失；
--   2) 动作要按人审计（谁、说了什么、解析成什么、结果），内存 deque 无法查询、无法对账；
--   3) 告警确认与处置任务要跨角色、跨端同步，必须有唯一真源。
--
-- 五条硬约束写在库里：
--   1) `control_actions.status` 只允许七态；重复确认由应用层条件 UPDATE 命中 0 行守住；
--   2) `audit_ops` 只追加：不提供 UPDATE/DELETE 代码路径，`phase` 区分"想做"与"做完"；
--   3) `assistant_alerts` 一条预警只有一条告警记录（UNIQUE warning_id），多副本用
--      ON CONFLICT DO NOTHING 收敛；
--   4) `handling_tasks.version` 是乐观锁，认领/移交必须带 expected_version；
--   5) `operators` 不插默认账号：席位由 seed 脚本或管理接口初始化。
--
-- 回滚（设计文档 §7.3）：DROP TABLE audit_ops → handling_tasks → handling_spaces
--   → assistant_alerts → control_actions → operators。

CREATE TABLE IF NOT EXISTS operators (
    operator_id  text PRIMARY KEY CHECK (operator_id ~ '^[A-Za-z0-9_-]{4,32}$'),
    display_name text NOT NULL CHECK (display_name <> '' AND length(display_name) <= 32),
    role         text NOT NULL CHECK (role IN ('duty', 'lead', 'admin')),
    active       boolean NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE operators IS
    '值班席位（声明身份，非强认证）；不插默认账号，由 seed 脚本或 POST /operators 初始化';

CREATE TABLE IF NOT EXISTS control_actions (
    action_id       text PRIMARY KEY CHECK (action_id ~ '^act_[0-9a-f]{12,24}$'),
    operator_id     text NOT NULL REFERENCES operators(operator_id),
    action          text NOT NULL,
    space_id        text,
    utterance       text NOT NULL DEFAULT '',
    resolved_args   jsonb NOT NULL DEFAULT '{}'::jsonb,
    status          text NOT NULL CHECK (
        status IN ('proposed', 'executing', 'executed', 'failed', 'rejected', 'expired', 'revoked')
    ),
    idempotency_key text UNIQUE,
    impact_scope    jsonb NOT NULL DEFAULT '{}'::jsonb,
    result          jsonb,
    error           jsonb,
    expires_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE control_actions IS
    '一次动作请求的状态机：写动作先 proposed，确认后 executing → executed/failed';

CREATE INDEX IF NOT EXISTS ix_actions_operator ON control_actions (operator_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_actions_status ON control_actions (status, updated_at);
CREATE INDEX IF NOT EXISTS ix_actions_space ON control_actions (space_id, created_at DESC);

CREATE TABLE IF NOT EXISTS assistant_alerts (
    alert_id     text PRIMARY KEY,
    warning_id   text NOT NULL,
    level        int NOT NULL CHECK (level BETWEEN 1 AND 4),
    region_code  text NOT NULL,
    status       text NOT NULL CHECK (
        status IN ('raised', 'delivered', 'acked', 'escalated', 'merged', 'closed')
    ),
    ack_by       text REFERENCES operators(operator_id),
    ack_at       timestamptz,
    assigned_to  text REFERENCES operators(operator_id),
    snooze_until timestamptz,
    escalated_at timestamptz,
    drill        boolean NOT NULL DEFAULT false,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (warning_id)
);

COMMENT ON TABLE assistant_alerts IS
    '告警实例：一条预警一条记录；ack_by=确认人，assigned_to=当前处置人，drill=演练标识';

CREATE INDEX IF NOT EXISTS ix_alerts_open ON assistant_alerts (status, level, created_at DESC);

CREATE TABLE IF NOT EXISTS handling_spaces (
    space_id   text PRIMARY KEY,
    warning_id text NOT NULL UNIQUE,
    status     text NOT NULL CHECK (status IN ('open', 'closed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    closed_at  timestamptz
);

COMMENT ON TABLE handling_spaces IS
    '处置空间：一条预警一个空间（space_id = warning_id 的语义在应用层保证）';

CREATE TABLE IF NOT EXISTS handling_tasks (
    task_id    text PRIMARY KEY,
    space_id   text NOT NULL REFERENCES handling_spaces(space_id),
    stu_id     text NOT NULL,
    assignee   text REFERENCES operators(operator_id),
    status     text NOT NULL CHECK (status IN ('pending', 'claimed', 'in_progress', 'acked', 'closed')),
    version    int NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (space_id, stu_id)
);

COMMENT ON TABLE handling_tasks IS
    '处置任务：stu_id 是逻辑外键（STU 可能只在读视图/内存里，不加 FK）；version 是乐观锁';

CREATE INDEX IF NOT EXISTS ix_tasks_space_status ON handling_tasks (space_id, status);

CREATE TABLE IF NOT EXISTS audit_ops (
    id              bigserial PRIMARY KEY,
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
    ts              timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE audit_ops IS
    '动作审计流水：只追加；intent 在执行前，result 在执行后，两条共用 action_id';

CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_ops (ts DESC);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_ops (action_id, phase);

-- 回填按时间倒序取最近 N 条；没有这两个索引，启动回填会全表扫。
CREATE INDEX IF NOT EXISTS ix_warnings_generated ON warnings (generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_stu_created ON standardized_task_units (created_at DESC);
