# Dify 源码迁移清单（1.17.1）

> 取证基线：`langgenius/dify` tag `1.17.1`，commit `8387590ace4a094de812b7847fc6a4c3a27cd52b`（2026-09-10）。
> 取证方式：`git clone --depth 1 --single-branch --branch 1.17.1`（经由只读镜像 `gh-proxy.com`，因本机 github.com:443 不可达；`api.github.com` 复核 tag 指向同一 commit）。克隆落 `E:/tmp/dify-src`，工作树干净。
> 复核日期：2026-10-22。初核：Qoder agent；评审：待用户确认（批次 0 退出条件）。
> 本文件是白名单的唯一登记处：任何 T1/T2 文件必须在此有条目，反向亦然（许可守卫双向校验）。

## 1. 与计划的差异（以克隆为准）

| 计划表述 | 1.17.1 实际 | 处置 |
|---|---|---|
| `edges/custom-edge/index.tsx` | 文件为 `web/app/components/workflow/custom-edge.tsx`（另有 `custom-connection-line.tsx`） | 交互事实仍成立（`getBezierPath` + `curvature: 0.16`），仅更新路径引用 |
| `node-catalog.ts` | 1.17.1 无此文件；Start/End 的 block 定义在 `types.ts` 与 `block-selector/constants.tsx` | Start/End 独立 block 的事实成立，证据改引这两处 |
| 后端 `api/core/workflow/graph_engine/*`、`variable_pool.py` | 1.17.1 已外置为 Python 包 `graphon==0.7.0`（`api/pyproject.toml:48`），仓库内无 `core/workflow/graph_engine/` 与 `variable_pool.py`；`VariablePool` 由 `graphon.runtime` 提供 | 后端迁移结论不变（T3）；证据改为 `graphon==0.7.0` 依赖声明与 `from graphon.runtime import VariablePool` 调用点 |
| 剪贴板函数 `writeWorkflowClipboard` / `readWorkflowClipboard` | 均存在；另有 `stringifyWorkflowClipboardData`（L163-175） | 端口一并迁移 stringify；read/write 的浏览器 API 留 composable |
| `use-workflow-history` 上限 50 | 上游未显式设置 limit，使用 zundo 默认；去抖 500ms | AEGIS 显式设 50（计划口径），fixture 记录“去抖合并、future 清空、无显式上限”事实 |
| 快捷键“保存 `Mod+S`” | 1.17.1 的 `shortcuts/definitions.ts` 快捷键表**没有** save/help 项（全仓 `Mod+S` 只出现在数据集文档编辑器） | AEGIS 在保留子集外显式新增 `workflow.save`（`Mod+S`）与 `workflow.help`（`?`），inventory T1-2 与 fixture 已登记 |
| 节点宽度 | `NODE_WIDTH = 240`（`constants.ts:7`），卡片 `w-60`（`nodes/_base/node.tsx:202`） | 240px 正确；`NODE_FOOTPRINT` 同步改 240 |

已抽查存在的 UI 证据文件：`header/header-in-normal.tsx`、`header/run-and-history.tsx`、`panel/index.tsx`（`top-14 right-0 bottom-1` 见 L126）、`block-selector/index.tsx`、`block-selector/tabs.tsx`、`nodes/_base/node.tsx`、`run/index.tsx`、`run/result-panel.tsx`、`run/node.tsx`、`panel/inputs-panel.tsx`、`panel/debug-and-preview/user-input.tsx`、`block-selector/constants.tsx`、`note-node/index.tsx`。

## 2. 分级定义

- **T1 直接迁移**：纯 TypeScript，无 React/React Flow/Zustand/jotai/i18n/Dify 节点类型依赖。保留函数体与边界分支，只换类型、常量、归属头；用 fixture 对拍。
- **T2 语义迁移**：与框架耦合，按行为契约在 Vue 3 + Vue Flow + Pinia 重写；保留状态机、快捷键表、边界条件与测试用例。
- **T3 原则上不迁移**：Dify 领域节点、后端引擎、DSL、i18n 文案包、布局库。**例外（2026-10-23 用户批准）**：工作流 UI 的 JSX 结构、CSS 设计值与界面文案可逐块移植为 Vue SFC，见 §3.4；Dify Logo/品牌资产仍禁止使用。

## 3. 白名单逐项

### 3.1 T1（直接迁移）

| # | 上游文件与函数（行号） | AEGIS 落点 | 改造要点 | fixture |
|---|---|---|---|---|
| T1-1 | `web/app/components/workflow/utils/clipboard.ts`：`sanitizeClipboardValueByDefault` L37-84；`isClipboardValueCompatibleWithDefault` L86-108；`isClipboardNodeStructurallyValid` L110-118；`isClipboardEdgeStructurallyValid` L120-128；`parseWorkflowClipboardText` L130-161；`stringifyWorkflowClipboardData` L163-175；`writeWorkflowClipboard` L177-183；`readWorkflowClipboard` L185-194 | `frontend/src/utils/graph/clipboard.ts`（纯解析/净化/结构校验）+ `frontend/src/composables/workflow/useWorkflowClipboard.ts`（navigator 适配） | `Node/Edge` → `NodeDef/EdgeDef`；payload `kind=aegis-workflow-clipboard`、`version=1`；失败返回显式错误（不静默吞）；新增 2MB 上限与 `__proto__/prototype/constructor` 过滤；`__proto__` 作为普通键丢弃而非赋值 | `clipboard.json` |
| T1-2 | `web/app/components/workflow/shortcuts/definitions.ts`：`WORKFLOW_CANVAS_SHORTCUTS` L48-154；`getWorkflowCanvasShortcutDisplayKey` L156-165 | `frontend/src/components/workflow/shortcuts/definitions.ts` | `@tanstack/react-hotkeys` 类型换本地 `HotkeyChord/IndividualKey`；保留 save/delete/copy/paste/duplicate/undo/redo/V/H/organize/zoom/fit；去掉 `workflow.comment-mode`、`workflow.download-import-log`、`workflow.dim-other-nodes`；新增 `workflow.save`（`Mod+S`）与 `workflow.help`（`?`） | `shortcuts.json` |
| T1-3 | `web/app/components/workflow/utils/version.ts`：`getWorkflowVersionName` L6-14 | `frontend/src/components/workflow/version/version-name.ts` | 类型换 AEGIS 版本 DTO；三分支不变（marked_name → `# n` → 默认名） | `version-name.json` |
| T1-4 | `web/app/components/workflow/utils/keyboard-movement.ts`：`getKeyboardMovement` L3-18 | `frontend/src/components/workflow/editor/keyboard-movement.ts` | React `KeyboardEvent` → DOM 类型；Alt/Ctrl/Meta 直接返回、Shift 20px、默认 5px 原样保留 | `keyboard-movement.json` |
| T1-5 | `web/app/components/workflow/utils/edge.ts`：`getEdgeColor` L3-20 | `frontend/src/components/workflow/editor/edge-color.ts` | 映射改 AEGIS `NodeState`；颜色改 `--aeg-*` token；fail branch 语义保留；**偏差**：AEGIS 无 Exception，由 `timeout` 承接并折叠为危险色（fixture `aegisCases` 覆盖全状态） | `edge-color.json` |
| T1-6 | `web/app/components/workflow/utils/workflow.ts`：`getNodesConnectedSourceOrTargetHandleIdsMap` L43-105 | `frontend/src/components/workflow/editor/connections.ts` | 适配 Vue Flow add/remove change；保留 source/target handle 集合更新；移除上游 `!` 非空断言与 `any`；**偏差**：删除不存在的 handle 时上游 `findIndex=-1` 会 `splice(-1,1)` 误删末尾，AEGIS 改为 no-op（fixture 覆盖） | `connections.json` |
| T1-7 | `web/app/components/workflow/utils/node.ts`：`genNewNodeTitleFromOld` L110-121；`getTopLeftNodePosition` L123-137 | `frontend/src/utils/graph/node-factory.ts` | 两函数迁移；标题递增后缀保留、基名由调用方给中文；落点按 240px 卡宽；`getNestedNodePosition` 依赖容器，不迁移；**偏差**：空集返回 `null` 而非 `Infinity`（fixture 注明） | `node-factory.json` |
| T1-8 | `web/app/components/workflow/utils/variable.ts`：`variableTransformer` L4-8；`normalizeWorkflowOutputName` L10 | `frontend/src/utils/graph/variable.ts` | 双向转换固定 AEGIS 语法 `{{#node.path#}}`；`isExceptionVariable` L12-14 不迁移（AEGIS 无 Dify error handle） | `variable.json` |
| T1-9 | `web/app/components/workflow/utils/common.ts`：`formatWorkflowRunIdentifier` L7-20 | `frontend/src/components/workflow/run/format.ts` | 逻辑原样；fallback 改中文“运行中”；fixture 固定 `locale=zh-CN`、`timeZone=Asia/Shanghai` | `format.json` |

### 3.2 T2（语义迁移）

| # | 上游文件与行为（行号） | AEGIS 落点 | 保留的契约 | fixture |
|---|---|---|---|---|
| T2-1 | `web/app/components/workflow/utils/node-navigation.ts`：定位数学 L93-96（`x + innerWidth*0.25`、`y + innerHeight*0.25`、`zoom=1`、`duration=800`）；事件封装 L19-108 | `frontend/src/composables/workflow/useNodeNavigation.ts` | 目标节点落在视口左上 1/4；节点不存在不抛；不用 document 自定义事件，改 Pinia action + Vue Flow `setViewport` | `node-navigation.json` |
| T2-2 | `web/app/components/workflow/hooks/use-workflow-history.ts`：事件枚举 L14-31；500ms 去抖 L66-75；保存分支 L77-109；标签 L111-147；`store/workflow/index.ts:75` zundo temporal（未显式 limit） | `frontend/src/composables/workflow/useWorkflowHistory.ts` + `frontend/src/utils/graph/history.ts` | past/present/future；拖动在 `dragstop` 合并一次；事件标签；AEGIS 显式上限 50；undo/redo 只替换文档；redo 后清空 future；纯快照逻辑下沉 domain | `history.contract.json` |
| T2-3 | `web/app/components/workflow/hooks/use-selection-interactions.ts`：L11-131（选择框开始/变化/拖动/取消/右键） | `frontend/src/composables/workflow/useCanvasSelection.ts` | 框选、Shift 点选增减、点空白清空；只读模式不移动 | `selection.contract.json` |
| T2-4 | `hooks/use-nodes-available-var-list.ts` L40-101、L103-175；`hooks/use-workflow-variables.ts` L20-121 | `frontend/src/composables/workflow/useUpstreamVariables.ts` | 只保留“祖先节点 + catalog 输出键 + 上次运行输出”的过滤/分组；不做 env/conversation/snippet/tool 变量；非祖先过滤、无输出空态、分组顺序 | `upstream-variables.contract.json` |
| T2-5 | `hooks/use-workflow-organize.helpers.ts`：L21-138（容器尺寸、层对齐、位置应用） | `frontend/src/composables/workflow/useAutoLayout.ts` + `frontend/src/utils/graph/layout.ts` | 对齐/间距语义；布局复用现有 `layerPositions`，不引入 elkjs；自动整理后无重叠、方向不变、整理后 fit | `auto-layout.contract.json` |

### 3.3 T3（不迁移，仅参考）

| 上游 | 结论 | 理由 |
|---|---|---|
| `utils/workflow-init.ts` L77-400（迭代/循环预处理、环检测、初始节点/边） | 不迁移 | AEGIS 无 iteration/loop 容器节点；环检测与拓扑层已在 `workflow/model.py` 的 `find_cycle`/`topological_levels`；只参考其“先验校验再加载”顺序 |
| `utils/elk-layout.ts`（`DEFAULT_NODE_WIDTH=244`、`DEFAULT_NODE_HEIGHT=100`、`layered`/`RIGHT`、`nodeNodeBetweenLayers=100`、`nodeNode=80`） | 不迁移 | 引入 elkjs 违背“不新增运行时依赖”；AEGIS 已有 `layerPositions`；若批次 B spike 证明不足，单独立项评估 |
| Dify 后端 `graphon==0.7.0`（GraphEngine / VariablePool / GraphRuntimeState） | 不迁移 | 与 Dify ORM/LLM/节点实体强耦合；AEGIS 已有 DAG 状态机、核签、在途改图、SLA、版本化。`graphon` 不是本仓依赖，不引入第二套引擎 |
| `nodes/_base/node.tsx`、`block-selector/*`、`panel/*`、`run/*`、`header/*`、`custom-edge.tsx`、`note-node/*` | 按 §3.4 逐文件移植 UI 结构与设计值 | 2026-10-23 用户批准；不整包嵌入 React/Next，不使用 Dify Logo/品牌资产；DOM 与状态管理重写为 Vue |

### 3.4 UI 源码移植（2026-10-23 用户批准）

> 复制范围仅限工作流 UI 的结构、CSS 设计值与界面文案；每文件首部保留归属头，notices/inventory 双向登记。
> Dify Logo、名称、截图与版权信息不得用于冒充 AEGIS 品牌。

| # | 上游文件 | AEGIS 落点 | 改造要点 |
|---|---|---|---|
| U1 | `header/header-in-normal.tsx`、`header/index.tsx` | `frontend/src/components/workflow/editor/EditorHeader.vue` | 56px 顶栏；返回/标题/保存态/版本/试运行/发布；React 组件换 Vue |
| U2 | `index.tsx` 画布组合、`style.css` | `frontend/src/components/workflow/editor/EditorCanvas.vue` | Vue Flow；14px 点阵；面板 `top:56px; right:0; bottom:0` 浮层关系 |
| U3 | `block-selector/index.tsx`、`block-selector/blocks.tsx`、`block-selector/constants.tsx` | `frontend/src/components/workflow/editor/BlockSelector.vue` | 400px 弹层；搜索/分组/最近使用/键盘；Start 已存在禁用 |
| U4 | `nodes/_base/node.tsx` | `frontend/src/components/workflow/editor/WorkflowNode.vue` | 240px 宽、16px 圆角、图标/标题/摘要/状态环；handle 上移 16px |
| U5 | `custom-edge.tsx`、`utils/edge.ts` | `frontend/src/components/workflow/editor/WorkflowEdge.vue` | 贝塞尔 2px、状态色、条件标签、中点 “+” 插入 |
| U6 | `note-node/index.tsx` | `frontend/src/components/workflow/editor/NoteNode.vue` | 220×140、双主题、只进 layout |
| U7 | `nodes/_base/components/workflow-panel/index.tsx` | `frontend/src/components/workflow/editor/NodePanel.vue` | 400px 浮层；字段/动作插槽；关闭归还焦点 |
| U8 | `web/app/components/workflow/style.css`、Tailwind token | `frontend/src/styles/theme.css`、`frontend/src/theme/tokens.ts` | 设计值转 `--aeg-*` 双主题 token，硬编码色计数为 0 |

## 4. 后端对照结论（graphon 0.7.0）

- 1.17.1 的 `api` 依赖 `graphon==0.7.0`（`api/pyproject.toml:48`），`VariablePool` 由 `graphon.runtime` 导入（如 `api/core/app/apps/workflow/app_runner.py:37`、`advanced_chat/app_runner.py:53`）。
- 仓库内不再有可逐文件迁移的 `graph_engine` 源码；`tests/unit_tests/core/workflow/graph_engine/` 只是调用侧测试目录。
- 结论：**不引入 graphon，也不复制其算法**。AEGIS 的 `workflow/engine.py` 继续是唯一编排路径；仅把“dry-run/preview 包装”作为应用层能力补在现有引擎外侧（批次 E）。
- 可借鉴但不迁移的算法：拓扑就绪队列、变量池路径解析。AEGIS 已在 `model.py` 有 `topological_levels`，变量引用将按本计划 `{{#node.path#}}` 自研（批次 D）。

## 5. 合规与守卫落点

- 归属头（每个 T1/T2 文件首部）：

```ts
// Ported from Dify web/app/components/workflow/<path>
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS NodeDef/EdgeDef and --aeg-* tokens; see THIRD_PARTY_NOTICES.md.
```

- `THIRD_PARTY_NOTICES.md`：Dify LICENSE 全文、来源 URL/tag/commit、文件清单与修改说明。
- 许可守卫：`frontend/src/utils/graph/__tests__/license.spec.ts` 双向校验本清单与文件归属头；生产代码禁止出现 `dify-workflow-clipboard`（fixture 目录除外）。
- 禁止复制：Dify Logo/品牌资产、Zustand/jotai store、React Flow hooks、Dify 节点实现、Dify DSL、Dify 后端引擎/变量池。UI 源码按 §3.4 白名单逐文件移植，禁止使用 Dify 名称/截图冒充 AEGIS。
- 单租户假设：AEGIS 按内部单租户部署；对多租户/对外分发前必须重新做法务评审（修改版 Apache-2.0 的第 1 条）。

## 6. 未决与后续批次

| 事项 | 状态 | 批次 |
|---|---|---|
| T1/T2 fixture 实际采集 | 批次 0 完成（见 `frontend/src/utils/graph/__fixtures__/dify-1.17.1/`） | 0 |
| 归属头随端口文件落盘 | 未开始（端口文件尚不存在） | B/C/D/F |
| 许可守卫双向扫描 | 批次 0 建立骨架；端口文件落盘后自动生效 | 0/B/C/D/F |
| 上游漂移 | 固定 1.17.1；升级重克隆、重跑对拍、更新本清单与 notices，单独 PR | 持续 |
| 工具链可复现 | 批次 0 完成：`uv lock --check`/`uv sync --frozen`/`npm ci` 均通过；`pytest-cov` 与 scoped mypy 已入 pyproject | 0 |
| legacy fixtures | 批次 0 完成：`backend/tests/fixtures/workflow/legacy/` 8 份旧定义 + manifest，7 条用例校验签名与可读性 | 0 |
| ADR-0009/0010 | 批次 0 已落盘，待用户评审；评审通过前不进入批次 A | 0 |
| 变更记录 | 2026-10-22 初版（克隆复核）；同日补 T1/T2 fixture、许可守卫、工具链与 legacy fixtures；2026-10-23 用户批准 UI 源码移植，新增 §3.4 白名单并修订 T3 口径 | 0/B |
