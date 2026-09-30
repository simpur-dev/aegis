-- 003_indexes：访问路径。全部 IF NOT EXISTS，可重复应用。

-- 热点查询：某区域某时窗的读数回放（前端地图回放与规则复核都走这条）。
-- 列序按"等值在前、范围在后"：region_code 等值 + observed_at 区间，DESC 便于 limit 直接反向取。
CREATE INDEX IF NOT EXISTS idx_telemetry_region_time ON telemetry_readings (region_code, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_station_time ON telemetry_readings (station_id, observed_at DESC);

-- 空间：站点半径检索（ST_DWithin 走 GIST 做边界框预筛，见 geo.py）。
CREATE INDEX IF NOT EXISTS idx_stations_geom ON monitoring_stations USING gist (geom);
CREATE INDEX IF NOT EXISTS idx_stations_region ON monitoring_stations (region_code);

-- 向量：HNSW 而非 IVFFlat —— IVFFlat 建索引要先有足量样本喂 k-means 训练 lists，
-- 边端首周近空表建它等于建了个退化的全扫；HNSW 增量可建、在线召回稳定，冷启动即有效。
CREATE INDEX IF NOT EXISTS idx_task_embedding_hnsw ON standardized_task_units USING hnsw (embedding vector_cosine_ops);

-- 预警检索：区域 + 时间（与遥测同构的热点），以及按链路的反查。
CREATE INDEX IF NOT EXISTS idx_warnings_region_time ON warnings (region_code, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_warnings_trace ON warnings (trace_id);
CREATE INDEX IF NOT EXISTS idx_warnings_codes ON warnings USING gin (region_codes);
CREATE INDEX IF NOT EXISTS idx_warnings_hazard_time ON warnings (hazard_type, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_receipts_warning ON warning_receipts (warning_id, attempted_at DESC);

-- STU：事件反查（by_event）与区域时间窗。
CREATE INDEX IF NOT EXISTS idx_tasks_event ON standardized_task_units (event_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_region_time ON standardized_task_units (region_code, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_hazard ON standardized_task_units (hazard_type, created_at DESC);

-- 链路台账：按完成时间倒序取最近链路（观测面板）。
CREATE INDEX IF NOT EXISTS idx_chains_finished ON chain_runs (finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_chains_warning ON chain_runs (warning_id) WHERE warning_id IS NOT NULL;

-- 工作流：版本链与按链路反查实例；节点态按 state 建偏索引（人工核签待办是热查询）。
CREATE INDEX IF NOT EXISTS idx_wf_def_name_version ON workflow_definitions (name, version DESC);
CREATE INDEX IF NOT EXISTS idx_wf_instance_trace ON workflow_instances (trace_id);
CREATE INDEX IF NOT EXISTS idx_wf_instance_status_time ON workflow_instances (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wf_node_state ON workflow_node_states (state) WHERE state = 'awaiting_human';
