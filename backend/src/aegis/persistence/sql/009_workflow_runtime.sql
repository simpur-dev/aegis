-- P1-5：工作流定义补齐族谱/内容指纹，实例补齐"重启后能否恢复"的运行态现场。
--
-- 定义表原先只按 (name, version) 存图：恢复后 lineage 只能按插入顺序猜，版本指纹也丢了。
-- 实例表原先只有节点状态：等待点的提示/选项、人工决策、画布位置，以及运行中插节点/改参数
-- 之后的那张图都不在库里；没有它们，重启只能把实例当"跑丢了"处理，而不能续接人工工单。
ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS lineage_id text NOT NULL DEFAULT '';
ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS content_hash text NOT NULL DEFAULT '';

ALTER TABLE workflow_definitions
    ADD CONSTRAINT wf_def_lineage_shape CHECK (lineage_id = '' OR lineage_id ~ '^wf_[0-9a-f]{12}$');

ALTER TABLE workflow_instances ADD COLUMN IF NOT EXISTS runtime jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE workflow_instances
    ADD CONSTRAINT wf_instance_runtime_is_object CHECK (jsonb_typeof(runtime) = 'object');

COMMENT ON COLUMN workflow_definitions.lineage_id IS
    '版本族 ID：同名模板的全部版本共享；首次创建时取第一版 workflow_id（ADR-0009 §2）';
COMMENT ON COLUMN workflow_definitions.content_hash IS
    '发布文档的 canonical hash（含 layout）；旧行留空，仓储恢复时按定义投影补算';
COMMENT ON COLUMN workflow_instances.runtime IS
    '恢复现场：定义快照 + lineage + 人工决策 + 等待提示/选项 + 画布位置（WorkflowRuntime）';
