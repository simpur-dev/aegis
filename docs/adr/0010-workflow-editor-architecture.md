# ADR-0010：工作流编辑器的分层、状态与 Dify 交互语法迁移

- 状态：已采纳（批次 0 立项，2026-10-22；实现随批次 B/C/D/F 分批落地）
- 日期：2026-10-22
- 修订：2026-10-23 用户批准 Dify UI 源码移植，新增 §11，并同步修订 §背景 约束 1、§8 T3 与备选方案表
- 相关：ADR-0009（文档生命周期）、ADR-0004（引擎不换）、ADR-0008（模块边界与 AST 守卫的做法）、
  `docs/audit/DIFY_MIGRATION_INVENTORY.md`、`THIRD_PARTY_NOTICES.md`

## 背景

现有 `/workflow` 是一个能跑但难改的编辑器：`views/WorkflowView.vue` 与 `components/workflow/*`
把加载、执行、画布、表单、轮询混在一起；`stores/workflow.ts` 是单一大 store；领域类型、wire DTO、
Vue Flow 类型之间大量 `as` 直通；没有草稿/发布/版本，没有节点目录，也没有分层与格式门禁。
结果是“看起来能用，但每加一个交互都要动全身”。

目标不是换成 Dify 的产品，而是**借它的交互语法**（全屏画布、240px 节点卡、边中 “+”、块选择器、
运行面板 RESULT/DETAIL/TRACING、草稿/发布/版本）重建一个 AEGIS 自己的编辑器：
节点、核签、SLA、在途改图、审计语义全部保留。迁移边界受两条硬约束：

1. **许可**：Dify 是修改版 Apache-2.0，前端 LOGO/版权不可去除，多租户需另行授权。AEGIS 按内部单租户
   部署。2026-10-23 用户批准 UI 源码移植：在保留归属头、许可全文并标注修改的前提下，可以把 Dify 的
   JSX 结构、CSS 设计值与界面文案逐块移植为 Vue SFC；Dify Logo/品牌资产不得充当 AEGIS 品牌；
   DSL、Zustand/jotai store 与后端引擎仍不迁移。详见 §11。
2. **不新增运行时依赖**：Vue Flow、Pinia、VueUse、Ant Design Vue、ECharts 已在仓内；ELK、Dify SDK、
   `graphon` 不引入。新增的只有开发期工具链。

## 决策

### 1. 三层依赖，方向用机器守住

```
views/WorkflowView.vue           composition root：路由、EditorContext、生命周期
components/workflow/*            UI：Vue / Ant Design Vue / Vue Flow / store 公开面
stores/workflow/*                application 状态：Pinia；只存可序列化数据
composables/workflow/*           application 工具：Vue/VueUse + domain + contract
utils/graph/*                    domain：纯 TS；零框架依赖
api/workflow/*                   transport：contract / codec / axios client
```

| 层 | 允许 | 禁止 |
|---|---|---|
| `utils/graph` | 标准库、同层 | `vue`、`pinia`、`axios`、`@vue-flow/*`、`ant-design-vue`、`@/api`、`@/stores`、`@/components` |
| `composables/workflow` | `vue`、`@vueuse/core`、`utils/graph`、`api/workflow/contract` | `axios`、`@/stores`、`@/components`、`api/workflow/client` |
| `stores/workflow` | `vue`、`pinia`、`@vueuse/core`、`utils/graph`、`composables/workflow`、`api/workflow/contract` | `axios`、`@/components`、`api/workflow/client` |
| `components/workflow` | `vue`、`ant-design-vue`、`@vue-flow/*`、domain 类型、stores、composables | `axios`、`api/workflow/client` |
| `api/workflow/client.ts` | `axios`、`contract`、`codec`、domain 类型 | `@/components`、`@/stores`、`vue` |
| `views/WorkflowView.vue` | 以上公开面 | `api/workflow/client` 直调 |

规则不是靠自觉：ESLint `no-restricted-imports` 管 `.ts/.vue`，`utils/graph/__tests__/architecture.spec.ts`
用 TypeScript AST 再扫一遍 import 图，防止注释/动态 import 绕过。禁止 `any`、`!` 非空断言、`v-html`。

### 2. 契约先行：unknown 进，domain 出

- `api/workflow/contract.ts` 定义 `WorkflowGateway`（方法面按资源分组）与 `WorkflowErrorCode`；
  **不 import axios/vue**。stores/composables 只依赖接口，测试注入 fake gateway。
- `codec.ts` 对每个响应做 `unknown` 解析：校验 `schema_version`、必填、类型、枚举、数组上限；
  错误带路径（如 `nodes[3].config.sla_seconds`）。**禁止 `as WorkflowDocument` 直通**。
- `client.ts` 是仓库里 workflow 侧唯一的 axios 出口；把 HTTP/错误体归一为 `WorkflowApiError(code, status, details)`。
  UI 用中文码表渲染，不显示裸后端字符串。旧端点的错误形状由 codec 归一。
- **不做前端代码生成**：接口面小，运行时解析更能发现漂移；OpenAPI 侧用契约测试断言路径/字段/枚举/
  错误形状（ADR-0009 §11），前端不引入生成器与生成产物。
- 取消与超时：gateway 接受 `AbortSignal`；普通请求 20s、preview/run 60s；组件卸载/模式切换取消在途请求。

### 3. 四个 store，一个 EditorContext，没有神 store

删除现有 `stores/workflow.ts`，拆为四个 setup store（均以 `createXStore(gateway)` 工厂支持测试注入）：

- `document`：文档、保存状态、自动保存、发布/恢复、冲突；
- `run`：实例、preview、轮询、核签/改参/插节点/旁路/中止；
- `catalog`：节点目录、加载/重试、未知类型只读降级；
- `editor`：模式、选择、面板、视口等**易失 UI 状态**。

`components/workflow/editor/context.ts` 用 typed `provide/inject` 注入四个 store 与 editor actions；
画布子树不直接 import 全局单例，组件测试与 E2E 用 provider 注入 fake。`WorkflowView.vue` 是唯一
composition root。**store 只存可序列化数据**：Vue Flow 实例用 `shallowRef`/`markRaw` 留在视图层，
不进 store。不保留兼容 re-export，旧 import 一次性改完。

### 4. 文档修改只走命令；历史只快照文档

- `applyCommand(document, command) → document` 是纯 reducer；非法命令抛 `DocumentError`，store 不提交。
  组件不直接改 `nodes/edges`。命令：`addNode/removeNodes/moveNodes/connect/removeEdges/updateNode/
  insertOnEdge/paste/rename/setStartVariables/setNotes`。
- 创建 ID 的命令通过参数接收 `newId`：domain 不读全局随机源，测试可确定。
- 历史：`past/present/future` 快照栈，只含文档（不含选择/视口/悬停）；拖动在 `dragstop` 合并为一次；
  上限 50；redo 后清空 future；undo/redo 只替换文档，不做增量重放。
- 模式（draft/version/instance）各自持有独立文档快照；切换只能走显式 action，单测断言
  “切到实例再回草稿，草稿字节不变”。`focusInstance` 不再覆盖草稿，返回草稿不重新拉取。

### 5. 视图适配层隔离 Vue Flow

`components/workflow/editor/graph-adapter.ts` 负责 `WorkflowDocument ↔ Vue Flow nodes/edges`；
domain 不出现 Vue Flow 类型。拖拽时只更新视图局部坐标，`dragstop` 才提交 `moveNodes`；
视口 `moveend` 300ms 防抖写回 layout。节点 240px（`NODE_FOOTPRINT` 同步），卡片高度与手柄布局由视图决定。
分支节点按 `rules[].then + default` 派生多 source handle，`graphToDef` 把 handle 写回
`EdgeDef.condition`；阈值等单出口节点保持单 handle。

### 6. 自动保存、恢复与冲突

- 800ms 去抖自动保存，带 `baseRevision`；请求序号 + `AbortController` 防止迟到响应覆盖新文档。
- `409 revision_conflict` 进入 `conflict` 并暂停自动保存，UI 提供“载入服务器版本/覆盖（二次确认）”。
- 网络错误指数退避 1s/2s/4s，最多 3 次；`Mod+S` 立即 flush；`beforeunload` 仅在有脏改动时提示。
- localStorage 只存**恢复日志**（lineage/revision/document hash），刷新后提示“恢复未保存草稿”，
  用户确认才载入；服务器草稿仍是真源，不在本地保存文档正文。

### 7. 轮询与运行态

`usePolling` 基于 `useIntervalFn` + `useDocumentVisibility`：运行中 1s、等待人工 5s；
一次只有一个在途请求；终态停止；组件卸载清理；测试用 fake timers。
运行面板同侧 420px（或底部 40% 抽屉），三页签 RESULT/DETAIL/TRACING；核签、改参、插节点、旁路、中止
全部复用现有端点，UI 不新造执行语义。

### 8. Dify 迁移分级与偏差登记

基线固定 `langgenius/dify` tag `1.17.1`，commit `8387590ace4a094de812b7847fc6a4c3a27cd52b`；
克隆取证与逐文件行号见 `docs/audit/DIFY_MIGRATION_INVENTORY.md`。

- **T1 直接迁移**：纯 TS 逻辑（剪贴板解析/净化、快捷键表、版本名、键盘位移、边色、连接集合、
  标题/落点、变量名转换、运行标识格式化）。保留函数体与边界分支，只换类型/常量/归属头，用 fixture 对拍。
- **T2 语义迁移**：与框架耦合的行为（节点定位、历史、框选、上游变量、自动整理）。按契约在 Vue 3 +
  Vue Flow + Pinia 重写，保留状态机与边界条件，用契约 fixture 断言。
- **T3 原则上不迁移**：Dify 节点与 DSL、Zustand/jotai、后端 `graphon` 引擎、`elkjs` 布局。
  **例外（2026-10-23 用户批准）**：Dify 工作流 UI 的 JSX 结构、CSS 设计值与界面文案可逐块移植为
  Vue SFC，但必须保留归属头、登记 inventory/notices，且不得使用 Dify Logo/品牌资产；详见 §11。

已登记的上游偏差与修正：

- 上游剪贴板净化会把 `__proto__` 赋到目标对象上（原型污染）；AEGIS 把它当普通键丢弃，并拒绝
  `prototype/constructor` 路径；payload 改为 `kind=aegis-workflow-clipboard`，加 2MB 上限与显式错误。
- 上游“移除不存在的 handle”用 `splice(-1, 1)` 会误删最后一个 handle；AEGIS 改为 no-op（fixture 覆盖）。
- 上游快捷键表**没有**保存/帮助项；AEGIS 新增 `workflow.save`（`Mod+S`）与 `workflow.help`（`?`）。
- 上游节点历史用 zundo 默认上限；AEGIS 显式 50。上游节点宽 240px，与计划一致，不改。

许可合规：每个 T1/T2 文件首部保留归属头；`THIRD_PARTY_NOTICES.md` 收录 Dify LICENSE 全文、来源、
文件清单与修改说明；`license.spec.ts` 双向校验“清单 ↔ 归属头”，并禁止 Dify 剪贴板 kind 进入生产代码。
升级上游必须重克隆、重跑 fixture、更新清单与 notices，单独 PR。

### 9. 交互外壳与可访问性

- `/workflow` 路由 `meta.immersive=true`，`App.vue` 整块不渲染侧栏/顶栏/服务令牌条/页脚
  （助手入口自 2026-10-23 起也在侧栏里，随侧栏一起消失，不再是浮在正文上的胶囊）；
  编辑器自带头部（56px）：返回/可编辑标题/保存态/版本/运行态/定义/刷新/保存/发布/更多。
- 画布占满剩余空间；右面板 400px 浮层不挤压画布，从头部下沿起；备注节点 220×140 存 layout、不参与执行；
  边中 “+” 插入并自动重接，双击空白或工具条打开块选择器（400px）。
- **边中标签层（条件胶囊 + “+”）压在节点之上**（`.ed-canvas .vue-flow__edge-labels{z-index:1001}`）：
  库里这一层是 `z-index:auto`，而每棵节点带内联 z-index（选中/拖动那棵 1000），真机量到默认竖排的
  两颗节点之间那颗 “+” 被上一张卡的文字盖住、hit-test 命中节点子树——"在中间插一步"按不到。
- 内容驱动的面板开合与头部按钮的开合**是两件事**：选中/插入节点走 `revealPanel`（已开着就只换档），
  头部「运行态/定义」走 `openPanel`（同档再点一次是关）。写成一支会把"连加两颗节点"变成
  "第二颗刚选中就把面板关掉"（真机就红在这条上：动线判据等 `panel-close` 等到超时）。
- 状态不只靠颜色：成功/失败/运行中/等待人工/降级都有图标与文字；失败节点有错误徽标；
  节点 `role="button"` + `aria-label`，Enter 打开面板，方向键移动，Escape 关闭并归还焦点；
  面板/dialog 焦点陷阱；保存/运行 `aria-live="polite"`；`prefers-reduced-motion` 关闭脉冲
  （头部"轮询中"那颗脉冲点也在名单里，漏一条就是开了减动效还在动）。
- 只用 `--aeg-*` token；workflow 新目录硬编码颜色计数必须为 0；`uiTheme.spec.ts` 基线不得上升。
- 低 zoom 的可读性靠 `--canvas-stroke-k`（1/zoom，封顶 3）补偿：连线 1.5px、卡片描边 1px、类别条 3px
  都乘这一档。**选择器必须挂在现役的 `.ed-canvas` 上**——这批曾经把它留在退役前的 `.wf__canvas` 上，
  于是整条补偿静默失效（真机 fit 到 0.48 时连线只剩 0.97 设备像素），门禁按设备像素判才抓得住。
- 视觉验收断点 1440/1280/1024/768，浅色/深色各截图；关键动线 Playwright 断几何/对比度，不靠肉眼
  （深色那一趟连节点卡本身也要量：底色亮度、标题压在底色上的对比、连线压在画布底上的图形对比）。

### 10. 性能与门禁

- 上限 200 节点/500 边；节点/边数组 `computed` 派生；避免 deep watch 大数组；路由懒加载编辑器；
  重面板 `defineAsyncComponent`；`manualChunks` 保证 Cesium 不进 workflow chunk。
- 前端门禁：`lint:workflow`、`format:check:workflow`、`typecheck`、`vitest`、`test:coverage`、`build`；
  覆盖率阈值只挂在 `utils/graph`、`composables/workflow`、`stores/workflow`、`api/workflow/codec.ts`
  （行/函数/语句 ≥90%，分支 ≥85%）。
- 架构守卫：ESLint 分层 + AST import 图 + license 双向校验 + 主题基线。
- 性能 harness（批次 F）：200 节点/199 边，首轮只建基线，回归用相对阈值（>2× 基线才红），读数进 `docs/REPORT.md`。

### 11. UI 源码移植（2026-10-23 修订）

用户明确批准“复制 Dify 的 React/CSS/资产/文案，再适配改造”。本修订把原先的 T3 UI 禁令收窄为
“禁止整包嵌入与品牌冒用”，允许按文件移植：

- **复制范围**：`web/app/components/workflow/` 下的 header、block-selector、panel、nodes/_base、
  custom-edge、note-node 等 UI 结构；Tailwind 设计值（尺寸/颜色/间距/圆角/阴影）；中文界面文案。
- **适配方式**：JSX → Vue 3 SFC；Tailwind class → scoped CSS + `--aeg-*` token；Dify store/hook →
  现有四 store 与 Vue Flow 适配层；Dify i18n key → AEGIS 文案常量。**不新增 React/Next/Tailwind
  运行时**，不把 Dify 页面整包挂载为第二套前端。
- **仍然禁止**：使用 Dify 名称、Logo、截图或版权信息冒充 AEGIS 品牌；复制 Dify DSL、Zustand/jotai
  store、`graphon`/后端引擎；删除或修改 Dify 源码中的版权与许可声明。
- **许可义务**：每个移植文件首部保留归属头（上游路径、tag/commit、版权、许可、修改摘要）；
  `THIRD_PARTY_NOTICES.md` 保留 LICENSE 全文与文件清单；商业分发/多租户前重新做法务评审
  （修改版 Apache-2.0 第 1 条与外观专利声明）。
- **验收**：视觉/交互以 Dify 1.17.1 为参照，但 DOM、组件名、状态管理与数据契约必须是 AEGIS 自己的；
  新目录硬编码颜色计数仍为 0，主题、焦点、键盘与 reduced-motion 门禁不降级。

## 备选方案与取舍

| 方案 | 判定 | 理由 |
|---|---|---|
| 整包嵌入 Dify React/Next 前端 | 不采用 | 双 UI 运行时、Next 与 Dify store/DSL 强耦合；按 §11 逐块移植 UI 源码并适配 Vue，保留许可与归属 |
| 引入 Dify DSL + `graphon` 后端 | 不采用 | 第二套引擎、强耦合 Dify ORM；AEGIS 的在途改图/核签/SLA 无法原样保留 |
| 保留单一大 store，渐进拆分 | 不采用 | 现有 store 已经同时管文档/运行/画布；延迟拆分只会让新交互继续堆进去 |
| 引入 ELK 自动布局 | 不采用 | 新增运行时依赖；现有 `layerPositions` 足够，若 B 阶段 spike 证明不足再单独立项 |
| 由 OpenAPI 生成前端客户端 | 不采用 | 接口面小；生成器增加工具链与漂移面，运行时 unknown 解析更直接 |
| 用 `as` 断言快速接上类型 | 不采用 | 会把后端漂移变成运行期崩溃；codec 路径化错误正是为了在边界处暴露 |
| 继续用 Vue Flow 类型作为领域类型 | 不采用 | 领域层被框架绑死，无法单测、无法复用、无法做纯历史快照 |

## 后果

- 正面：UI 与领域解耦；文档修改可单测、可回放；四个 store 各自可注入 fake；契约漂移在边界报出路径；
  迁移逻辑有来源、许可、对拍与偏差记录；交互语法与 Dify 对齐但节点/执行/核签是 AEGIS 自己的。
- 代价：批次 B 需要一次性删除旧 store/视图并更新所有 import，短期 diff 较大；T1/T2 端口要维护归属头与
  fixture；覆盖率阈值要求新目录持续保持测试密度；自动布局能力弱于 ELK（用现有算法换取零新依赖）。
- 待办：批次 B 落分层与 store 拆分；C/D/F 落 T1/T2 端口与守卫；E2E `workflow-editor.spec.ts`；
  性能基线进 `docs/REPORT.md`；Dify 升级时重跑全部 fixture 并更新 notices。
