-- 002_schema：业务表结构。列口径与 `aegis.domain.messages` 的领域记录严格一一对应，
-- 本层不引入第二套行模型：映射函数（persistence/rows.py）是唯一的双向翻译处。
-- 时间一律 timestamptz：链路口径全部为 UTC，落库带时区可避免边缘设备所在时区歧义。

-- 监测站点/行政区维表：域模型 TelemetryReading 只带 station_id，坐标的唯一事实源在此。
-- 读数不复制坐标，站点搬迁后历史读数不失真；空间检索经 station 联结（见 persistence/geo.py）。
CREATE TABLE IF NOT EXISTS monitoring_stations (
    station_id   text PRIMARY KEY,
    name_zh      text NOT NULL DEFAULT '',
    region_code  text NOT NULL,
    hazard_focus text[] NOT NULL DEFAULT '{}',
    geom         geography(Point, 4326),
    elevation_m  double precision,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT stations_region_code_shape CHECK (region_code ~ '^[0-9A-Z]{6,24}$'),
    CONSTRAINT stations_elevation_range CHECK (elevation_m IS NULL OR elevation_m BETWEEN -500 AND 9000)
);

COMMENT ON COLUMN monitoring_stations.geom IS 'WGS84 站点坐标；geography 使半径/距离以米为口径（见 geo.py 的取舍说明）';

-- 遥测读数：高原弱网下的批量写入主体（edge -> buffer -> COPY 暂存 -> 幂等插入）。
-- 无业务主键，故以自然键 (station_id, metric, observed_at) 做去重位：
-- 弱网"至少一次重传"同一读数时幂等吸收，不会放大任何计数。
CREATE TABLE IF NOT EXISTS telemetry_readings (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    station_id   text NOT NULL,
    metric       text NOT NULL,
    value        double precision NOT NULL,
    unit         text NOT NULL,
    region_code  text NOT NULL,
    observed_at  timestamptz NOT NULL,
    ingested_at  timestamptz NOT NULL,
    source       text NOT NULL DEFAULT 'simulator',
    quality_flag text NOT NULL DEFAULT 'ok',
    inserted_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT telemetry_region_code_shape CHECK (region_code ~ '^[0-9A-Z]{6,24}$'),
    CONSTRAINT telemetry_quality_flag CHECK (quality_flag IN ('ok', 'suspect', 'missing', 'drift')),
    CONSTRAINT telemetry_natural_key UNIQUE (station_id, metric, observed_at)
);

COMMENT ON COLUMN telemetry_readings.ingested_at IS '平台落库时刻；与 observed_at 之差即摄取时延，边缘时钟回拨可为负，指标侧按绝对值口径处理';

-- 灾害事件 / 标准化任务单元（STU v1）：决策智能体产出、工作流引擎消费。
-- deadline_at 用生成列：它是 created_at 与 sla_seconds 的纯函数，
-- 单独存一份会与两个源列漂移，生成列让"不可能不一致"由数据库保证。
-- embedding 供后续检索模块回填（预警知识/剧本相似召回），空表期不影响写入。
CREATE TABLE IF NOT EXISTS standardized_task_units (
    task_unit_id          text PRIMARY KEY,
    schema_version        text NOT NULL DEFAULT '1.0',
    event_id              text NOT NULL,
    hazard_type           text NOT NULL,
    region_code           text NOT NULL,
    task_type             text NOT NULL,
    objective             text NOT NULL,
    priority              smallint NOT NULL,
    sla_seconds           integer NOT NULL,
    owner_role            text NOT NULL,
    required_capabilities text[] NOT NULL,
    trigger_refs          text[] NOT NULL DEFAULT '{}',
    outputs               text[] NOT NULL DEFAULT '{}',
    dependencies          text[] NOT NULL DEFAULT '{}',
    input_data_refs       jsonb NOT NULL DEFAULT '[]'::jsonb,
    fallback_policy       jsonb NOT NULL,
    context_snapshot      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by            text NOT NULL,
    created_at            timestamptz NOT NULL,
    -- 生成列必须走 UTC 往返：`timestamptz + interval` 与 make_interval 在 Postgres 里都是
    -- STABLE（考虑夏令时），直接写会报 "generation expression is not immutable"。
    -- 先 AT TIME ZONE 'UTC' 降到无时区 timestamp 再算、算完升回，因本层时间一律 UTC 而完全等价。
    deadline_at           timestamptz GENERATED ALWAYS AS (((created_at AT TIME ZONE 'UTC') + make_interval(secs => sla_seconds)) AT TIME ZONE 'UTC') STORED,
    embedding             vector(1024),
    embedded_model        text,
    inserted_at           timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tasks_priority_range CHECK (priority BETWEEN 1 AND 5),
    CONSTRAINT tasks_sla_range CHECK (sla_seconds BETWEEN 1 AND 86400),
    CONSTRAINT tasks_region_code_shape CHECK (region_code ~ '^[0-9A-Z]{6,24}$'),
    CONSTRAINT tasks_capabilities_nonempty CHECK (cardinality(required_capabilities) >= 1),
    CONSTRAINT tasks_id_shape CHECK (task_unit_id ~ '^stu_[0-9a-f]{16}$')
);

COMMENT ON COLUMN standardized_task_units.embedding IS 'vector(1024)：bge-m3 口径；写入方是检索模块，链路本身只落结构化列';

-- 预警产物：status/delivered_at/reach_seconds 是从 releases_at 与回执集合派生的观测位。
-- reach_seconds 不做生成列：它跨表依赖 warning_receipts 的 max(receipt_at)，
-- 生成列只能引用本行列，故由映射层按 WarningRecord.reach_seconds() 同源写入。
CREATE TABLE IF NOT EXISTS warnings (
    warning_id          text PRIMARY KEY,
    event_id            text NOT NULL,
    trace_id            text NOT NULL,
    hazard_type         text NOT NULL,
    region_code         text NOT NULL,
    region_codes        text[] NOT NULL,
    risk_level          smallint NOT NULL,
    status              text NOT NULL,
    title_zh            text NOT NULL,
    body_zh             text NOT NULL,
    body_bo             text,
    audiences           text[] NOT NULL DEFAULT '{}',
    channels            text[] NOT NULL DEFAULT '{}',
    translation_pending boolean NOT NULL DEFAULT false,
    generated_at        timestamptz NOT NULL,
    released_at         timestamptz,
    delivered_at        timestamptz,
    reach_seconds       double precision,
    inserted_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT warnings_region_codes_nonempty CHECK (cardinality(region_codes) >= 1),
    CONSTRAINT warnings_risk_level_range CHECK (risk_level BETWEEN 1 AND 5),
    CONSTRAINT warnings_primary_region_shape CHECK (region_code ~ '^[0-9A-Z]{6,24}$'),
    CONSTRAINT warnings_status CHECK (status IN ('draft', 'released', 'delivered', 'partial', 'failed')),
    CONSTRAINT warnings_reach_nonneg CHECK (reach_seconds IS NULL OR reach_seconds >= 0),
    CONSTRAINT warnings_id_shape CHECK (warning_id ~ '^wrn_[0-9a-f]{20}$')
);

COMMENT ON COLUMN warnings.region_code IS '主责任区域 = region_codes 首元素；单值列用于免数组展开的高频过滤';
COMMENT ON COLUMN warnings.status IS 'draft 未发布 / released 已发布待回执 / delivered 全通道回执 / partial 部分回执 / failed 全通道失败';

-- 通道回执：DeliveryAttempt 的关系化落位（一对多），自然键同样为弱网重传兜底。
CREATE TABLE IF NOT EXISTS warning_receipts (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    warning_id     text NOT NULL REFERENCES warnings (warning_id) ON DELETE CASCADE,
    channel        text NOT NULL,
    audience_count integer NOT NULL,
    status         text NOT NULL,
    attempted_at   timestamptz NOT NULL,
    receipt_at     timestamptz,
    provider_msg_id text,
    CONSTRAINT receipts_audience_nonneg CHECK (audience_count >= 0),
    CONSTRAINT receipts_status CHECK (status IN ('pending', 'delivered', 'failed', 'retried')),
    CONSTRAINT receipts_natural_key UNIQUE (warning_id, channel, attempted_at)
);

-- 链路运行台账：record_chain 的落库面。阶段耗时与降级原因是观测型载荷，按 jsonb 归档。
-- 复合主键 (trace_id, event_id) 即契约 ID：同一链路重放幂等。
CREATE TABLE IF NOT EXISTS chain_runs (
    trace_id      text NOT NULL,
    event_id      text NOT NULL,
    ok            boolean NOT NULL,
    acted         boolean NOT NULL,
    stages        jsonb NOT NULL DEFAULT '[]'::jsonb,
    risk          jsonb,
    hits          jsonb NOT NULL DEFAULT '[]'::jsonb,
    task_unit_ids text[] NOT NULL DEFAULT '{}',
    warning_id    text,
    errors        text[] NOT NULL DEFAULT '{}',
    degradations  text[] NOT NULL DEFAULT '{}',
    finished_at   timestamptz NOT NULL,
    PRIMARY KEY (trace_id, event_id),
    CONSTRAINT chains_trace_shape CHECK (trace_id ~ '^trc_[0-9a-f]{16}$'),
    CONSTRAINT chains_event_shape CHECK (event_id ~ '^evt_[0-9a-f]{12}$'),
    CONSTRAINT chains_stages_is_array CHECK (jsonb_typeof(stages) = 'array')
);

COMMENT ON TABLE chain_runs IS '只追加的审计台账：ChainResult 载荷含 aegis.services 的类型，持久层不得反向导入，故不回读重建';

-- 工作流定义（不可变版本链）：与 workflow.store.WorkflowRepository 的内存索引同构。
-- signature 落库便于"同名模板修订后是否真的换图"在库侧可比对，不必反序列化整棵树。
CREATE TABLE IF NOT EXISTS workflow_definitions (
    workflow_id  text PRIMARY KEY,
    name         text NOT NULL,
    description  text NOT NULL DEFAULT '',
    version      integer NOT NULL,
    status       text NOT NULL DEFAULT 'active',
    created_by   text NOT NULL DEFAULT 'platform.workflow',
    nodes        jsonb NOT NULL,
    edges        jsonb NOT NULL DEFAULT '[]'::jsonb,
    signature    text NOT NULL,
    inserted_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT wf_def_version_range CHECK (version >= 1),
    CONSTRAINT wf_def_status CHECK (status IN ('active', 'archived')),
    CONSTRAINT wf_def_nodes_is_array CHECK (jsonb_typeof(nodes) = 'array'),
    CONSTRAINT wf_def_id_shape CHECK (workflow_id ~ '^wf_[0-9a-f]{12}$'),
    CONSTRAINT wf_def_name_version_key UNIQUE (name, version)
);

-- 工作流实例：定义与实例分离是"柔性"的落点，实例持有创建时的图快照。
CREATE TABLE IF NOT EXISTS workflow_instances (
    instance_id      text PRIMARY KEY,
    workflow_id      text NOT NULL,
    workflow_version integer NOT NULL,
    trace_id         text NOT NULL,
    status           text NOT NULL,
    error            text,
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    results          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL,
    finished_at      timestamptz,
    CONSTRAINT wf_instance_status CHECK (status IN ('running', 'waiting', 'succeeded', 'failed', 'aborted')),
    CONSTRAINT wf_instance_version_range CHECK (workflow_version >= 1),
    CONSTRAINT wf_instance_id_shape CHECK (instance_id ~ '^wfi_[0-9a-f]{12}$')
);

-- 节点运行态：WorkflowInstance.nodes(dict[str, NodeRun]) 的关系化落位，一对多。
-- 逐节点建行而非整包 jsonb：重排/插桩后的状态要能按 state 直接检索与统计。
CREATE TABLE IF NOT EXISTS workflow_node_states (
    instance_id          text NOT NULL REFERENCES workflow_instances (instance_id) ON DELETE CASCADE,
    node_id              text NOT NULL,
    type                 text NOT NULL,
    state                text NOT NULL,
    attempts             integer NOT NULL DEFAULT 0,
    schedule_latency_ms  double precision,
    duration_ms          double precision,
    output               jsonb NOT NULL DEFAULT '{}'::jsonb,
    error                text,
    notes                text[] NOT NULL DEFAULT '{}',
    updated_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instance_id, node_id),
    CONSTRAINT node_state_value CHECK (
        state IN ('pending', 'ready', 'running', 'succeeded', 'failed', 'skipped', 'bypassed', 'degraded', 'awaiting_human', 'cancelled', 'timeout')
    ),
    CONSTRAINT node_attempts_nonneg CHECK (attempts >= 0)
);
