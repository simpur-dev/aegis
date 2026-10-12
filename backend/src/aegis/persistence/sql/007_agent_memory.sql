-- 智能体共享记忆（§4.3 / D5）：群体认知对齐的实体载体。幂等建表，可反复执行。
--
-- 为什么必须有这张表：论文的"共同的记忆模块"要的是**跨事件、跨智能体**的经验闭环——
-- assess 把研判写进来、feedback 把效果验证教训写进来、下一次同区域的 assess 再检索引用。
-- 在这张表存在之前，`services/memory.py` 只有进程内的 `InMemoryAgentMemory`：
-- 每次重启归零、多实例之间互不可见，"共享记忆"这句话只在下一次 import 之前成立（2026-10-12 对盘实测）。
--
-- 刻意不存系统的最终结论副本：研判结论本体在 `warnings`/`chain_runs`，这里只存
-- "智能体自己学到的、别处取不到的那一句"（content），并用 refs 把话头指回证据；
-- 于是同一条结论不会有两份可以对不上的事实。
--
-- 四类记忆里 episodic/semantic/knowledge 分别复用案例库/pgvector 检索/Graphiti（§4.3），
-- 都不在这张表里再造一份；这张表只承载新增的 `agent_memory` 命名空间。
CREATE TABLE IF NOT EXISTS agent_memory (
    record_id     text PRIMARY KEY,
    -- 写这条记忆的智能体 id（契约里的 agent_id，形如 assess_real01）；空串=平台代写，不是"未知"。
    agent_id      text NOT NULL DEFAULT '',
    -- 枚举与 `services/memory.py:SharedKind` 同源，三个值之外一律拒：
    -- 门禁/报表要按 kind 分账，掺进第四个值就是一部没人能对账的账。
    kind          text NOT NULL CHECK (kind IN ('assessment', 'disagreement', 'lesson')),
    -- 长度上限与 pydantic 模型 (max_length=2000) 一致：DB 与模型两处都写死同一个数，
    -- 谁改了另一边就会响亮失败，而不是静默截断出两种"同一条记忆"。
    content       text NOT NULL CHECK (content <> '' AND length(content) <= 2000),
    -- 区划码与 monitoring_stations.region_code / warning_truth_labels 同口径（6–24 位字母数字），
    -- 否则"同区域历史研判"这条检索永远配不上；可空表示全局经验（不绑定区域）。
    region_code   text CHECK (region_code IS NULL OR region_code ~ '^[0-9A-Z]{6,24}$'),
    -- 证据指针（读数 id / 案例 id / 预警 id）。存 jsonb 文本数组：与 refs 的语义一致，
    -- 不做外键——引用的对象可能先于记忆被清理，链路要能带着"指过的东西"继续讲下去。
    refs          jsonb NOT NULL DEFAULT '[]'::jsonb,
    -- 双盲仲裁的分歧标记（D10）：分歧本身是要沉淀的样本，不能只活在一条消息里。
    disagreement  boolean NOT NULL DEFAULT false,
    -- 这条经验的适用时刻（不是入库时刻）：教训可能讲的是几小时前的一次过火响应。
    valid_at      timestamptz NOT NULL,
    written_at    timestamptz NOT NULL DEFAULT now(),
    -- 语义检索面（§4.3 点名 pgvector）：维度与 bge-m3 口径同为 1024，写法同 002_schema。
    -- 允许 NULL 是诚实的：没配检索权重时向量就是没算，读侧据此退到"按时间倒序+过滤"，
    -- 而不是拿一条随机向量冒充语义命中。
    embedding     vector(1024),
    embedded_model text NOT NULL DEFAULT ''
);

COMMENT ON TABLE agent_memory IS
    '智能体共享记忆命名空间（assessment/disagreement/lesson）；结论本体仍在 warnings/chain_runs，这里只存跨事件经验';
COMMENT ON COLUMN agent_memory.embedding IS
    'vector(1024)：bge-m3 口径，与 standardized_task_units 同维度；NULL=未配嵌入腿，检索退到过滤+时序';
COMMENT ON COLUMN agent_memory.valid_at IS '经验的适用时刻（可早于入库时刻）；排序与"同区域历史"窗口都以它为准';

-- 读路径就两种形态：同区域按时间倒序、跨区域按 kind 倒序——各配一支索引，不靠顺扫。
CREATE INDEX IF NOT EXISTS agent_memory_region_time_idx
    ON agent_memory (region_code, kind, valid_at DESC);
CREATE INDEX IF NOT EXISTS agent_memory_kind_time_idx
    ON agent_memory (kind, valid_at DESC);
