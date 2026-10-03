-- 触发条件规则库版本化（完善计划批次 C2 / ADR-0006）。
--
-- 为什么要这张表：`services/trigger_rules.py` 里的 9 条阈值此前只活在代码里，
-- 而它的注释自己就写着"待专家评审替换"——没有版本号、没有标定依据、没有审核人，
-- 就没有人能回答"现场用的到底是哪一版阈值"。判级错了要回滚，也只有版本化才回滚得动。
--
-- 三条硬约束写在库里而不是写在文档里：
--   1) 同一 rule_id 只允许一条 active 版本（部分唯一索引）：两条同时生效就是两套阈值一起判，
--      而报表上只会显示一个等级；
--   2) 等级、权重、组合模式、条件载荷的形状都在 CHECK 里，脏行进不了库；
--   3) 退役版本不删行：准确率回放要能按"当时生效的那一版"复算。
--
-- 阈值本身只有一份口径（架构铁律 4）：下面的种子数据与 `default_rulebook()` 逐字段相同，
-- 由 `tests/unit/test_trigger_rules_library.py` 解析本文件比对钉住——两处漂移一次门禁就红。
CREATE TABLE IF NOT EXISTS trigger_rules (
    rule_id         text NOT NULL,
    version         integer NOT NULL CHECK (version >= 1),
    status          text NOT NULL CHECK (status IN ('draft', 'active', 'retired')),
    hazard_type     text NOT NULL CHECK (hazard_type IN
                      ('landslide', 'rockfall', 'debris_flow', 'avalanche', 'lake_outburst', 'quake_triggered', 'unknown')),
    description     text NOT NULL CHECK (description <> ''),
    mode            text NOT NULL CHECK (mode IN ('all', 'any')),
    -- 1 最高（红）：与 domain.enums.RiskLevel 同口径，禁止本地另立分级
    triggered_level smallint NOT NULL CHECK (triggered_level BETWEEN 1 AND 5),
    weight          double precision NOT NULL CHECK (weight > 0 AND weight <= 10),
    -- [{metric, op, threshold, agg, window_seconds}, ...]：至少一条，算子与聚合在装配时逐条校验
    conditions      jsonb NOT NULL CHECK (jsonb_typeof(conditions) = 'array' AND jsonb_array_length(conditions) >= 1),
    calibration_basis text NOT NULL DEFAULT '',
    reviewer          text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL DEFAULT now(),
    activated_at      timestamptz,
    note              text NOT NULL DEFAULT '',
    PRIMARY KEY (rule_id, version)
);

COMMENT ON TABLE trigger_rules IS
    '触发条件规则库：草稿/生效/退役三态，同 rule_id 仅一条 active';

CREATE UNIQUE INDEX IF NOT EXISTS trigger_rules_single_active_idx
    ON trigger_rules (rule_id)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS trigger_rules_status_idx
    ON trigger_rules (status, hazard_type, rule_id, version);

-- 首版种子：即代码里的 9 条内置规则（标定依据栏如实写着"未经现场标定"）。
-- ON CONFLICT DO NOTHING 让"重复执行迁移"与"运维已经改过阈值"两件事互不干扰：
-- 已经被人动过的规则库不会被一次重启覆盖回去。
INSERT INTO trigger_rules (
    rule_id, version, status, hazard_type, description, mode, triggered_level, weight,
    conditions, calibration_basis, reviewer, activated_at
) VALUES
    ('R-DEBRIS-RAIN-1', 1, 'active', 'debris_flow', '短时强降雨激发泥石流', 'all', 2, 1.0, '[{"metric":"rain_10min","op":">=","threshold":30.0,"agg":"max","window_seconds":3600}]'::jsonb, '公开规范量级，未经现场标定', '平台内置（待专家评审）', now()),
    ('R-DEBRIS-RAIN-2', 1, 'active', 'debris_flow', '持续降雨叠加沟道泥位抬升', 'all', 1, 1.0, '[{"metric":"rain_cumulative_24h","op":">=","threshold":80.0,"agg":"max","window_seconds":86400},{"metric":"debris_level","op":">=","threshold":1.0,"agg":"max","window_seconds":1800}]'::jsonb, '公开规范量级，未经现场标定', '平台内置（待专家评审）', now()),
    ('R-LANDSLIDE-1', 1, 'active', 'landslide', '降雨入渗叠加位移加速', 'all', 2, 1.0, '[{"metric":"rain_cumulative_24h","op":">=","threshold":60.0,"agg":"max","window_seconds":86400},{"metric":"displacement_mm","op":">=","threshold":20.0,"agg":"max","window_seconds":3600}]'::jsonb, '公开规范量级，未经现场标定', '平台内置（待专家评审）', now()),
    ('R-LANDSLIDE-2', 1, 'active', 'landslide', '位移速率持续增加（蠕变加速阶段）', 'all', 3, 0.8, '[{"metric":"displacement_mm","op":">=","threshold":5.0,"agg":"rate","window_seconds":21600}]'::jsonb, '公开规范量级，未经现场标定', '平台内置（待专家评审）', now()),
    ('R-ROCKFALL-1', 1, 'active', 'rockfall', '冻融循环叠加危岩裂缝扩展', 'all', 2, 1.0, '[{"metric":"freeze_thaw_cycles","op":">=","threshold":3.0,"agg":"max","window_seconds":86400},{"metric":"crack_aperture_mm","op":">=","threshold":15.0,"agg":"max","window_seconds":21600}]'::jsonb, '公开规范量级，未经现场标定', '平台内置（待专家评审）', now()),
    ('R-AVALANCHE-1', 1, 'active', 'avalanche', '新雪叠加强风吹雪', 'all', 2, 1.0, '[{"metric":"new_snow_cm","op":">=","threshold":25.0,"agg":"max","window_seconds":21600},{"metric":"wind_speed_ms","op":">=","threshold":14.0,"agg":"max","window_seconds":3600}]'::jsonb, '公开规范量级，未经现场标定', '平台内置（待专家评审）', now()),
    ('R-AVALANCHE-2', 1, 'active', 'avalanche', '气温骤升致雪层弱层失稳', 'all', 3, 0.7, '[{"metric":"air_temperature_c","op":">=","threshold":2.0,"agg":"max","window_seconds":3600},{"metric":"snow_water_equivalent_mm","op":">=","threshold":40.0,"agg":"max","window_seconds":21600}]'::jsonb, '公开规范量级，未经现场标定', '平台内置（待专家评审）', now()),
    ('R-LAKE-1', 1, 'active', 'lake_outburst', '冰湖水位快速抬升', 'all', 2, 1.0, '[{"metric":"lake_level_m","op":">=","threshold":0.5,"agg":"max","window_seconds":21600}]'::jsonb, '公开规范量级，未经现场标定', '平台内置（待专家评审）', now()),
    ('R-LAKE-2', 1, 'active', 'lake_outburst', '水位抬升叠加坝体渗流浑浊', 'all', 1, 1.0, '[{"metric":"lake_level_m","op":">=","threshold":0.3,"agg":"max","window_seconds":86400},{"metric":"dam_seepage_turbidity_ntu","op":">=","threshold":50.0,"agg":"max","window_seconds":21600}]'::jsonb, '公开规范量级，未经现场标定', '平台内置（待专家评审）', now())
ON CONFLICT (rule_id, version) DO NOTHING;
