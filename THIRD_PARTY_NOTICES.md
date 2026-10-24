# Third-Party Notices

本文件登记 AEGIS 仓库中直接迁移或语义迁移的第三方源码、其许可文本、来源与修改说明。任何新增迁移文件必须先登记在此，再进入代码审查。

## Dify

- 项目：Dify
- 上游：https://github.com/langgenius/dify
- 版本：tag `1.17.1`，commit `8387590ace4a094de812b7847fc6a4c3a27cd52b`（2026-09-10）
- 迁移范围：`web/app/components/workflow/` 下的 T1 纯逻辑、T2 行为契约，以及经用户 2026-10-23 批准的 UI 源码移植（JSX 结构、CSS 设计值、界面文案 → Vue SFC）；逐文件清单见 `docs/audit/DIFY_MIGRATION_INVENTORY.md`。
- 未复制：Dify 的 Logo/品牌资产（不得冒充 AEGIS 品牌）、Zustand/jotai store、Dify 节点实现、Dify DSL、Dify 后端引擎/VariablePool（1.17.1 中已外置为 `graphon`，本仓库未引入）。UI 源码按文件移植并在文件首部标注来源与修改；不整包嵌入 React/Next 前端。
- 修改说明：迁移文件按 AEGIS 的 `NodeDef/EdgeDef` 类型、Vue 3 组件与 `--aeg-*` 设计 token 改写；剪贴板 payload kind 改为 `aegis-workflow-clipboard`；快捷键表删除 Dify 专属项；UI 文案统一为 AEGIS 中文界面用语；具体差异见每个文件首部归属头与迁移清单。
- 部署边界：AEGIS 按内部单租户部署使用；如未来对外提供多租户服务或分发，须重新进行法务评审（见许可第 1.a 条）。

### Dify LICENSE（全文，原样引用）

```text
# Open Source License

Dify is licensed under a modified version of the Apache License 2.0, with the following additional conditions:

1. Dify may be utilized commercially, including as a backend service for other applications or as an application development platform for enterprises. Should the conditions below be met, a commercial license must be obtained from the producer:

a. Multi-tenant service: Unless explicitly authorized by Dify in writing, you may not use the Dify source code to operate a multi-tenant environment.
    - Tenant Definition: Within the context of Dify, one tenant corresponds to one workspace. The workspace provides a separated area for each tenant's data and configurations.

b. LOGO and copyright information: In the process of using Dify's frontend, you may not remove or modify the LOGO or copyright information in the Dify console or applications. This restriction is inapplicable to uses of Dify that do not involve its frontend.
    - Frontend Definition: For the purposes of this license, the "frontend" of Dify includes all components located in the `web/` directory when running Dify from the raw source code, or the "web" image when running Dify with Docker.

2. As a contributor, you should agree that:

a. The producer can adjust the open-source agreement to be more strict or relaxed as deemed necessary.
b. Your contributed code may be used for commercial purposes, including but not limited to its cloud business operations.

Apart from the specific conditions mentioned above, all other rights and restrictions follow the Apache License 2.0. Detailed information about the Apache License 2.0 can be found at http://www.apache.org/licenses/LICENSE-2.0.

The interactive design of this product is protected by appearance patent.

© 2025 LangGenius, Inc.
```

### 迁移文件清单

> 状态：批次 0 仅登记落点；文件随批次 B/C/D/F 落盘时补“文件路径 / 上游路径 / 修改摘要 / 复核日期”。许可守卫会校验此清单与归属头双向一致。

| AEGIS 文件 | 上游文件 | 修改摘要 | 落盘批次 |
|---|---|---|---|
| `frontend/src/utils/graph/node-factory.ts`（2026-10-23 落盘） | `web/app/components/workflow/utils/node.ts` L110-137 | 标题递增/落点数学；去掉 React Flow 依赖；空集返回 null 而非 Infinity | B |
| `frontend/src/components/workflow/editor/edge-color.ts`（2026-10-23 落盘） | `web/app/components/workflow/utils/edge.ts` L3-20 | `NodeState` → `--aeg-*` token；timeout 承接上游 Exception，fail branch 保留 | B |
| `frontend/src/components/workflow/editor/connections.ts`（2026-10-23 落盘） | `web/app/components/workflow/utils/workflow.ts` L43-105 | Vue Flow change 适配；删除不存在的 handle 改 no-op（修复上游 -1 splice） | B |
| （待批次 C）`frontend/src/utils/graph/clipboard.ts` | `web/app/components/workflow/utils/clipboard.ts` L37-194 | AEGIS 类型/kind/大小上限/原型污染过滤 | C |
| （待批次 C）`frontend/src/components/workflow/shortcuts/definitions.ts` | `web/app/components/workflow/shortcuts/definitions.ts` L48-165 | 保留子集；本地热键类型 | C |
| （待批次 C）`frontend/src/components/workflow/editor/keyboard-movement.ts` | `web/app/components/workflow/utils/keyboard-movement.ts` L3-18 | DOM 事件类型 | C |
| （待批次 C）`frontend/src/composables/workflow/useWorkflowHistory.ts` + `frontend/src/utils/graph/history.ts` | `web/app/components/workflow/hooks/use-workflow-history.ts` L14-158 | 去抖合并/事件标签/50 步上限 | C |
| （待批次 C）`frontend/src/composables/workflow/useCanvasSelection.ts` | `web/app/components/workflow/hooks/use-selection-interactions.ts` L11-131 | Vue Flow 选择语义重写 | C |
| （待批次 C）`frontend/src/composables/workflow/useNodeNavigation.ts` | `web/app/components/workflow/utils/node-navigation.ts` L93-108 | Pinia action + `setViewport` | C |
| （待批次 D）`frontend/src/utils/graph/variable.ts` | `web/app/components/workflow/utils/variable.ts` L4-10 | `{{#node.path#}}` 双向转换 | D |
| （待批次 D）`frontend/src/composables/workflow/useUpstreamVariables.ts` | `hooks/use-nodes-available-var-list.ts` L40-175、`hooks/use-workflow-variables.ts` L20-121 | 祖先 + catalog 输出 + 上次运行 | D |
| （待批次 D）`frontend/src/composables/workflow/useAutoLayout.ts` + `frontend/src/utils/graph/layout.ts` | `hooks/use-workflow-organize.helpers.ts` L21-138 | 复用 `layerPositions`，不引入 elkjs | D |
| （待批次 F）`frontend/src/components/workflow/version/version-name.ts` | `web/app/components/workflow/utils/version.ts` L6-14 | AEGIS 版本 DTO | F |
| （待批次 B）`frontend/src/components/workflow/run/format.ts` | `web/app/components/workflow/utils/common.ts` L7-20 | fallback 中文化；固定 locale | F |

#### UI 源码移植（2026-10-23 用户批准）

> 这些文件按 Dify 1.17.1 的 JSX 结构与 Tailwind 设计值移植为 Vue SFC，保留文件首部归属头；
> 不复制 Dify Logo/品牌资产，也不整包嵌入 React/Next 运行时。

| AEGIS 文件 | 上游文件 | 修改摘要 | 落盘批次 |
|---|---|---|---|
| `frontend/src/components/workflow/editor/EditorHeader.vue` | `header/header-in-normal.tsx`、`header/index.tsx` | React 顶栏 → Vue 56px 编辑器头；AEGIS 保存/发布/版本动作 | B |
| `frontend/src/components/workflow/editor/EditorCanvas.vue` | `index.tsx` 画布组合 | React Flow 容器 → Vue Flow；点阵 14px、面板浮层关系 | B |
| `frontend/src/components/workflow/editor/BlockSelector.vue` | `block-selector/index.tsx`、`block-selector/blocks.tsx` | 400px 弹层、搜索/分组/最近使用；AEGIS 18 类节点与中文文案 | B |
| `frontend/src/components/workflow/editor/WorkflowNode.vue` | `nodes/_base/node.tsx` | 240px 卡、16px 圆角、图标/标题/摘要/状态环、handle 位置 | B |
| `frontend/src/components/workflow/editor/WorkflowEdge.vue` | `custom-edge.tsx` | Vue Flow 自定义边；2px、状态色、条件标签、中点 “+” | B |
| `frontend/src/components/workflow/editor/NoteNode.vue` | `note-node/index.tsx` | 220×140 备注节点；不进执行图 | B |
| `frontend/src/components/workflow/editor/NodePanel.vue` | `nodes/_base/components/workflow-panel/index.tsx` | 400px 右侧浮层基础版；字段/动作插槽 | B |
| `frontend/src/styles/theme.css`、`frontend/src/theme/tokens.ts` | `web/app/components/workflow/style.css` 及 Tailwind token 定义 | 设计值转 `--aeg-*` 双主题 token | B |
