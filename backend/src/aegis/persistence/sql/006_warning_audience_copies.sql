-- 006：预警的分受众文案（§3.4 三分支；契约按 D10"只增可选字段"演进）。
-- 为什么单独成文件而不是改 002：`schema_migrations` 记着每个迁移文件的 sha256，
-- 已应用的 002 一旦被改动，下一个已迁移的库启动时就会以"迁移文件发生漂移"炸在现场。
-- ADD COLUMN IF NOT EXISTS + NOT NULL DEFAULT '{}'：旧行拿到空对象，读侧不必区分"没有这列"。
ALTER TABLE IF EXISTS warnings
    ADD COLUMN IF NOT EXISTS audience_copies jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN warnings.audience_copies IS
    '按受众角色的差异化文案（execute 产出、链路逐分支过数字闸后才写）；空对象=未生成分支文案，与是否送达无关';
