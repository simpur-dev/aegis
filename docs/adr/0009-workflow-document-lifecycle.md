# ADR-0009：工作流文档、草稿、发布与预览的生命周期

- 状态：已采纳（批次 0 立项，2026-10-22；实现随批次 A/B/E 分批落地）
- 日期：2026-10-22
- 相关：ADR-0004（自研 DAG 与状态机，本 ADR 不换引擎）、ADR-0003（持久化后置项，
  #53 的替换点在本文 §7 明确）、`docs/audit/DIFY_MIGRATION_INVENTORY.md`（后端引擎不迁移的取证）、
  `backend/src/aegis/workflow/model.py`（现有 `WorkflowDef`/`WorkflowInstance`）

## 背景

现有工作流侧只有两类对象：**定义**（`WorkflowDef`，创建后不可变，修订产生新 `workflow_id`）
与**实例**（`WorkflowInstance`）。引擎、契约、前端画布都直接吃这两个形状。它满足“模板与在途实例分离”，
但缺三件编辑器必需的东西：

1. **没有草稿层**：画布上的每次拖动都对应一次“创建/修订定义”，没有“未发布的更改”这个概念；
2. **没有 lineage**：同一模板的多个版本只是同名但 ID 不同的定义，版本列表与运行历史无从按族聚合；
3. **没有版式与预览**：节点坐标、备注、画布视口没有归属；想“不改定义先试跑”只能真建实例并写账本。

Dify 1.17.1 的交互语法（草稿自动保存 → 显式发布 → 版本列表 → 试运行）证明这三层是编辑器可用性的底座，
而不是锦上添花。与此同时，AEGIS 的领域约束不能丢：已发布版本必须不可变、在途实例不受新发布影响、
运行中改图/核签仍走现有引擎、持久化边界不得假装已经落库。

## 决策

### 1. 一份文档，三种用途

`WorkflowDocument` 是草稿、预览、发布三种用途的**统一载荷**：

```
WorkflowDocument
  schema_version: 1
  lineage_id / name / description
  nodes: NodeDef[]
  edges: EdgeDef[]
  layout: WorkflowLayout
```

`WorkflowLayout` 是**兄弟对象，绝不塞进 `WorkflowDef`**：`positions/node_id→{x,y}`、`viewport`、
`notes`（备注节点）。理由是引擎的 `signature()` 只描述图身份，版式变化不应改变引擎眼中的模板；
版式又必须随版本可回溯，所以它属于文档与版本快照，但不属于引擎输入。

发布后仍生成不可变的 `WorkflowDef`（补 `lineage_id`/`schema_version`），`nodes/edges` 由文档投影而来；
引擎签名、调度、状态机与 ADR-0004 完全不变。**不引入第二套编排引擎，也不引入 `graphon`**：
1.17.1 的图引擎已外置为 `graphon==0.7.0`，与 Dify ORM/节点实体强耦合，取证见迁移清单 §4。

### 2. lineage 按 name 回填，版本只追加

- 同一 `name` 的最早版本 `workflow_id` 作为整条 lineage 的 `lineage_id`；仓储 `save()` 自动补齐，
  旧定义、内置模板、人工核签模板一律适用。
- 新空白草稿先分配一个 `wf_` 作为 lineage，首次发布复用它作为 v1，避免“先有版本才有族”的倒挂。
- `GET /lineages/{id}/versions` 返回**全部**版本（含归档），带节点/边数、创建时间、状态、`content_hash`、
  是否当前发布。归档不删除版本，只影响“能否继续发布/运行”的判定。
- 重命名在锁内原子更新整条 lineage 的 `_versions/_index/_latest`；重名 `409 name_conflict`。
  新建空白草稿走 `POST /lineages`；`PATCH /lineages/{id}` 作用于已有版本的 lineage——只有草稿、
  尚无版本时改名随首次发布生效（没有版本可同步，直接 404 而不是造一个假版本）。

### 3. 草稿是唯一的可变层

- 草稿对象：`lineage_id / base_workflow_id / revision / document / updated_at`，进程内保存。
- `PUT /lineages/{id}/draft` 必须带 `base_revision`；不匹配返回 `409 revision_conflict`，
  `details.server_revision` 供前端提示。**草稿写入不做 last-write-wins**。
- `revision` 单调递增；草稿校验在写入前完成，非法文档不入库。
- 恢复历史版本叫 `revert-to-draft`（旧版本 → 新草稿 revision），**不做破坏性回滚**：
  已发布版本与其运行历史永远保留，恢复只是把旧内容复制成新的可编辑草稿。

### 4. 发布是显式动作，且必须在锁内完成

- 只有“发布”才创建新版本；运行草稿不是发布，运行已发布版本才建正式实例。
- 锁内计算下一个版本号；两路并发发布只有一个成功，另一个 `409`。
- 与当前最新版本的内容哈希相同 → `409 no_changes`，不制造空版本。
- 已归档 lineage 发布 → `409 archived`；发布前必须通过文档校验（图语义、变量、容量）。
- 草稿自动保存、发布、恢复的关系：**发布与恢复都先落草稿层，再由草稿生成版本**，保证
  “画布看到的”和“要发布的内容”是同一份字节。

### 5. canonical hash 是内容身份的唯一判据

- `orjson.dumps(..., OPT_SORT_KEYS)` 的 UTF-8 字节 + `sha256`，前缀 `sha256:`。
- 纳入：`schema_version / name / description / nodes / edges / layout`。
- 排除：`workflow_id / version / created_at / revision / updated_at / lineage_id`。
- 理由是键序稳定、可跨进程复核；golden 测试固定一份文档的哈希，性质测试打乱键序后哈希不变。
- 前端**不重复实现** canonical hash：本地“有没有改过”用单调 `changeSeq` 对比 `savedSeq`；
  “内容是否相同”由服务端 `content_hash` 判定，避免两份实现漂移出不同的“无变化”结论。

### 6. 预览是 dry-run，不写任何账本

- `POST /previews` 接受当前文档或 lineage + payload，返回与 `instance_detail` 同构的结果。
- 独立 `_previews` 注册表：上限 8 条、TTL 30 分钟，过期清理；超出 `429/409 preview_limit`
  （按现有错误映射落到 4xx，不静默丢结果）。
- **不写**定义、不写实例、不写 `chain_runs`/审计账本；`ctx.dry_run=True` 把
  `notify/api_call/device_control/warning_publish/chain_stage` 包装为 `{dry_run, would_call}`。
- `human_review` 模拟选择第一个选项并标记 `simulated: true`，不产生人工待办；
  LLM 腿**照常执行**，UI 必须明示“试运行仍会调用模型”。
- spy 测试断言：0 次外部副作用、0 次 `HumanRequired`、0 条实例增量。

### 7. 持久化边界如实标注

- 定义、草稿、预览、实例仍全部在**进程内**；重启即清空（内置模板按原名重建）。
- `ports.py` 定义 `WorkflowRepositoryPort / DraftStorePort / PreviewStorePort / Clock / IdFactory`，
  内存实现由容器注入。它的作用是**为 #53 预留替换点**，不是宣称已经持久化。
- UI 必须显示“本次进程内，重启后清空”；ADR 与部署文档不得把内存实现写成持久能力。
- 接 Postgres 时只替换端口实现，应用层不改；这是 #53 的验收口径。

### 8. Start / End 与旧定义兼容

- 注册 `start`/`end` 两类节点：总 19 类，面板 18 类（`chain_stage` 仍是内部原语）。
- 约束：`start` 至多一个、无入边；存在 Start 时它是唯一根，所有可执行节点必须从 Start 可达；
  `end` 至多一个、无出边。
- **旧定义没有 Start 仍然合法**：运行输入回退为 JSON 表单；编辑器提供“一键补 Start”，
  但不在读取时偷偷改写旧文档。
- 新空白草稿自动带 Start；End 可省略（没有 End 时以所有叶子节点为终点）。

### 9. 变量语法与校验时机

- 语法固定 `{{#node_id.path#}}`，只解析 `NodeDef.config` 的**字符串叶子**；`EdgeDef.condition` 不解析。
- 整串引用保留原类型；内嵌到文本时只允许标量（字符串/数字/布尔）。
- 发布时硬校验：引用节点是祖先、语法合法、每节点引用数 ≤32、路径深度 ≤8；
  运行时再校验输出键并**点名**缺失路径（不静默变空串）。
- 拒绝 `__proto__ / prototype / constructor` 作为路径段或在剪贴板净化后残留；
  变量求值走自有纯函数，不展开到目标对象原型链。

### 10. 错误模型与容量边界

- 新端点统一 `{"detail": {"code", "message", "details"}}`；旧端点保持原形状，由前端 codec 归一。
- 错误码：`validation_failed | not_found | revision_conflict | name_conflict | no_changes | archived |
  preview_limit | invalid_schema_version | document_too_large | internal`；映射 400/404/409/422。
- 容量：节点 ≤200、边 ≤500、备注 ≤32、Start 变量 ≤32、单节点 config ≤64KB、文档序列化 ≤2MB、
  每节点变量引用 ≤32、路径深度 ≤8。超限 422 并指名路径。
- 日志只记 `lineage_id/workflow_id/revision/preview/trace_id` 与计数/哈希，不记 payload/config。

### 11. 后端分层与门禁

- 目录与依赖方向：`api/workflow → workflow/{documents,drafts,publishing,preview,variables} → domain`；
  `workflow` 不 import `fastapi`/`aegis.api`；`api` 不 import `workflow.store` 具体实现。
- 新模块在 `pyproject.toml` 有 scoped mypy 严格覆盖（`disallow_untyped_defs`、`warn_return_any`、
  `no_implicit_optional`）；覆盖率只统计新模块（含分支 ≥90%），不拿全仓历史债充数。
- 旧 `/definitions`、`/instances` 与核签/改参/插节点/旁路/中止保持兼容；新接口不破坏旧客户端。
  旧创建/修订已改走仓储锁内版本分配（同名再建仍按旧语义追加版本，不另立 lineage），
  并留下无版式文档快照；定义期重名冲突映射 409。
- OpenAPI 契约测试断言路径、必填字段、枚举、错误响应形状；不做前端代码生成（见 ADR-0010）。

## 备选方案与取舍

| 方案 | 判定 | 理由 |
|---|---|---|
| 直接采用 Dify DSL + `graphon` | 不采用 | 与 Dify ORM/节点实体/LLM 强耦合；AEGIS 已有核签、在途改图、SLA、版本化，引入即第二套引擎 |
| 把 `layout` 塞进 `WorkflowDef` | 不采用 | 版式变化会改变引擎签名与不可变对象；引擎不需要坐标 |
| 发布即覆盖当前版本 | 不采用 | 在途实例与审计要求版本不可变；覆盖会毁掉追溯 |
| 恢复 = 删除后续版本 | 不采用 | 破坏审计与运行历史；改为 `revert-to-draft` 追加草稿 |
| 运行草稿时隐式发布 | 不采用 | 用户会在不知情时制造版本；试运行必须无副作用 |
| 草稿写入 last-write-wins | 不采用 | 两个标签页会静默丢改动；`base_revision` 显式冲突更安全 |
| 先接 Postgres 再做草稿 | 不采用（顺序调整） | 草稿/发布语义与存储实现解耦；#53 通过端口替换，不阻塞编辑器 |

## 后果

- 正面：编辑器有“未发布更改”概念；版本可追溯且不可变；试运行零副作用；旧定义无 Start 仍可读；
  存储可替换而不改应用层；前后端对“有无变化”只有一个真源（服务端哈希）。
- 代价：多出 lineage/草稿/预览三组对象与一套错误模型；前端必须处理 409 冲突与恢复提示；
  进程内实现意味着重启清空，演示与评审时必须如实说明；哈希纳入 layout 意味着纯版式调整也会产生新版本
  （换取画布状态可回溯，这是有意的取舍）。
- 待办：#53 落库（替换 ports 实现，不改应用层）；批次 E 的 preview 包装与 spy；批次 A 的并发发布与
  revision 冲突用例；归档/恢复的完整状态机测试。
