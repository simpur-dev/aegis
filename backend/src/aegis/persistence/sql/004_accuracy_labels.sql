-- 预警准确率回放的**真值标注**侧（幂等：可反复执行，不重置已有数据）。
--
-- 为什么要有这张表：考核口径里的"多灾种预警准确率"必须把两边放在同一处才成立——
-- 一边是现场真值（该不该报、报的哪个灾种、什么级别），一边是系统实际产出的 `warnings`。
-- 在补这张表之前，真值只活在仓库外的 JSONL 里，"PostgreSQL 用于预警准确率回放"这句话
-- 其实只有"内存/文件读"那一半成立（2026-10-02 独立审计实测）。
--
-- 刻意**不**复制系统产出：predicted 侧一律 JOIN 现成的 `warnings` 表，
-- 于是同一场事件不会被存成两份可以对不上账的事实。
CREATE TABLE IF NOT EXISTS warning_truth_labels (
    case_id         text PRIMARY KEY,
    -- 区划代码与 monitoring_stations.region_code 同口径（6-24 位大写字母数字）：
    -- 否则标注永远 JOIN 不上站点与预警，回放会静默算成"全部漏报"。
    region_code     text NOT NULL CHECK (region_code ~ '^[0-9A-Z]{6,24}$'),
    hazard_type     text NOT NULL,
    -- 真值所属的观测时刻：predicted 侧以它为原点在一段时间窗内找系统产出。
    observed_at     timestamptz NOT NULL,
    truth_warning   boolean NOT NULL,
    truth_level     smallint CHECK (truth_level BETWEEN 1 AND 5),
    -- 出处与标注人：现场标注与事后复盘的可信度不同，报表要能把这个区分带出来。
    source          text NOT NULL DEFAULT '',
    labelled_by     text NOT NULL DEFAULT '',
    labelled_at     timestamptz NOT NULL DEFAULT now(),
    note            text NOT NULL DEFAULT ''
);

COMMENT ON TABLE warning_truth_labels IS
    '预警准确率回放的现场真值；predicted 侧 JOIN warnings，不在这里存副本';

-- 回放按时间窗扫描；region+hazard 的过滤走这张表的索引即可，无需外键（维表可后补）。
CREATE INDEX IF NOT EXISTS warning_truth_labels_region_time_idx
    ON warning_truth_labels (region_code, observed_at);
