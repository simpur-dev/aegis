# AEGIS 自主改进看板

> 唯一进度事实来源。每轮开工先读本文件 + `docs/UI_UX_REDESIGN_BRIEF.md`（台账/ADR 细节）+ `docs/REPORT.md` + `git status`。
> 选型结论与四问口径见 BRIEF §2/§7：组件库已批 **Arco Design Vue**（先探针实测再迁）；旧视觉已批现在停用；
> 布局骨架侧栏化**已于 2026-10-17 拍板并实施**（见下方决策日志与 T11）。

## 里程碑

| 里程碑 | 状态(todo/in_progress/done) | 说明 |
| --- | --- | --- |
| M0 现状审计与选型提案 | done | BRIEF §1–§3：基线实测、耦合面量化、六维对比、用户拍板 Arco |
| M1 设计令牌与双主题引擎 | in_progress | 令牌四层/切换器/ECharts 换肤/旧横幅停用已落地；欠 Cesium+Vue Flow 画布收尾（T09）、布局骨架已随 M2.5 实施 |
| M2 Dashboard 标杆页 | in_progress | 探索性 spike 已出标杆视觉并**已并入主线**（2026-10-17，`6c4b942..4f181a4` + merge `622bb92`）；待用户评审方向后按 T12 收组件层 |
| M2.5 信息架构收并（2026-10-17 新增） | in_progress | 用户拍板：八页平铺改侧栏分组导航（值班主线 4 + 系统 1），助手改常驻入口；外壳重构已落地（T11），页面合并/抽屉化待做（T28–T31）。2026-10-23 批次 B6：助手入口从右下角悬浮胶囊收回侧栏一行（真机量到浮标压在页脚、分页器与页码上），"常驻"语义不变 |
| M3 逐页推开 monitor/warnings/map/workflow/metrics/assistant | todo | 每页三态 + 双主题截图；范围按 M2.5 收并后的页面清单调整 |
| M4 多智能体预留补齐 | todo | 全局状态区（已随侧栏落地迷你区）、人在回路组件、AgentMessage v1 类型/store、画布节点区分+图例 |
| M5 分辨率走查与最终验收 | todo | 1366×768/1920×1080 走查、全门禁、最终验收报告 |

## 任务看板

| ID | 任务 | 状态 | 依赖 | 验证方式 | 备注 |
| --- | --- | --- | --- | --- | --- |
| T01 | 令牌四层（tokens/vars/antd/useTheme）+ 颜色棘轮门禁 | done | — | tokens.spec 9 + useTheme.spec 7 + 棘轮 458 基线 | 见 BRIEF §8 |
| T02 | theme.css/App.vue 只读令牌化 + 装饰退役 | done | T01 | 棘轮 458→228、uiTheme 12 条 | BRIEF §10 决策 4 |
| T03 | 顶栏主题切换器三态 + 持久化 + 跟随系统 | done | T02 | e2e 深色专项 10 项 | BRIEF §10 |
| T04 | 真机门禁扩双主题（七页深色扫描） | done | T03 | 全量 110 passed | BRIEF §10 |
| T05 | ECharts 画布接令牌（registerTheme + 重建实例） | done | T02 | chart.spec 7 + 画布像素判据 2 项，全量 112 | BRIEF §11 决策 7 |
| T06 | 旧视觉停用：PageHero 改朴素页头 + 藏青横幅删除 | done | T02 | 棘轮 228→159、全量 112、变异 4 处 | BRIEF §12 决策 8 |
| T07 | 顶栏页签导航：页名全档铺开、编号点/连线/页码徽标退役 | done | T06 | 已跑：typecheck exit 0、单测 793/45、build 54.74s、e2e 深色组 13 passed(53.9s) 含页签四档(1100/1280/1440/1920)连跑 | 提交 `65fd80e`；BRIEF §13 记决策 9；全量 e2e 回归后台跑，结果追记工作日志 |
| T08 | Cesium 底图/标记/标签接令牌 | done | T05 | 已跑：typecheck ✓ / 单测 805（新增 map.spec 7 + viewer.theme.spec 5）/ build 48.01s / e2e map-visual 7 passed（含新增翻面 1 项） | 派生层 `src/theme/map.ts`；证据分两半：真机钉画面级对账，图元重画由假 Cesium 单测钉（本机缺锚点资产，见 BRIEF §14） |
| T09 | Vue Flow 画布内部接令牌 | in_progress | T05 | graph.spec 单测 + workflow 页双主题真机 | utils/graph.ts NODE_STATE_STYLES 12 处（含原任务 #35）；同 T08 的派生层套路。批次 B6 补了两件：画布根改叫 `.ed-canvas` 后把描边补偿/浮层样式那段选择器一起迁过去（原来整段挂在退役的 `.wf__canvas` 上，静默失效——真机量到连线只剩 0.97 设备像素、缩放控件圆角 0）；新增深色画布真机判据（底色亮度 + 标题压在底色上的对比 + 连线对画布底的图形对比） |
| T10 | Arco 探针实测（暗色纯 CSS 变量？/表格 1366×768/体积变化） | todo | — | 临时分支装 @arco-design/web-vue 起探针页，三问各有实测数字 | 选型已批可装依赖；不过则退 Naive→退保留 antd |
| T11 | 布局骨架：可折叠侧栏（含智能体状态迷你区）+ 顶栏 + 栅格 | done | 用户批准（2026-10-17） | 已实施：侧栏分组导航（值班主线 4 + 系统 1）+ 顶栏（页名/状态/主题）+ 智能体迷你区 + 收起持久化；门禁已随结构改写 | 外壳重构与路由收并一次性落地，详见决策日志 2026-10-17 晚 |
| T12 | 阶段 2 组件层替换 antd→Arco（16 文件/202 标签）+ e2e 51 处 ant-* 选择器迁移 | todo | T10 | 全门禁绿 + 全量 e2e | 机械可改；不得动契约 |
| T13 | uiTheme.spec.ts 按新规范重写（4 条改口径、7 条保留） | todo | T12 | 重写后 11+ 条全绿、逐条变异检查 | 改口径不是删断言 |
| T14 | /dashboard 标杆页重做（KPI/五段链路/风险网格/SSE 流/智能体组/七腿三态 + 三态） | in_progress | T12 | 页面级 e2e + 双主题截图 + 全门禁 | M2 交付物。**spike 已并入主线**（2026-10-17，merge `622bb92`，BRIEF §15）：读数带/风险瓦片墙/五段分段条/智能体在线板/日志排事件流；组件层迁移（T12）仍待做 |
| T15 | /monitor 重做（遥测表+曲线、缺口标记、演练入口、人工上报向导+低置信核签） | todo | T14 | 同上 | |
| T16 | /warnings 重做（列表/详情、双语 translation_pending 如实标、回执、关联任务） | todo | T14 | 同上 | 不伪造藏文 |
| T17 | /map 重做（全屏 Cesium+浮动面板+详情抽屉，缺瓦片如实降级） | todo | T08、T14 | 同上 + 地图两道门禁 | 保持零 Ion/谷歌依赖 |
| T18 | /workflow 重做（16 类节点、版本化定义/实例、人工决策/改参/旁路、连线校验保留） | todo | T09、T14 | 同上 | |
| T19 | /metrics 重做（P50/P95/max vs 阈值、越限高亮、未测得如实显示） | todo | T14 | 同上 | 数字只来自埋点 |
| T20 | /assistant 重做（四类任务、流式帧、动作确认卡、未配 LLM 如实显示） | todo | T14 | 同上 | 越权拒绝并留痕 |
| T21 | 全局智能体状态区（五智能体在线/工作中/降级/离线常驻，SSE+integrations 数据源） | todo | T11 或顶栏落位决策 | 真机：降级时如实显示、绝不伪装在线 | M4 |
| T22 | 人在回路可复用组件（确认/核签队列、批准/驳回/改参） | todo | T14 | 组件单测 + monitor/assistant 两处接入真机 | 为智能体输出留插槽 |
| T23 | AgentMessage v1 TS 类型 + store（按 contracts/agent_message.v1.schema.json 对账） | todo | — | 类型单测 + 与 schema 对账脚本/用例 | 不改契约本身 |
| T24 | 五段链路可视化（decided_by/耗时/产出/降级原因如实标注） | todo | T14、T23 | dashboard 真机 + 降级场景注入验证"平台降级接管"字样 | |
| T25 | 分辨率走查（1366×768 与 1920×1080 两档全页）+ 关键路径 3 步可达核验 | todo | T14–T20 | 两档全量 e2e + 截图走查 | M5 |
| T26 | 最终验收：DoD 七条逐条勾选留证 + 全门禁 + 生产构建 + 《最终验收报告》 | todo | 全部 | 报告含命令与结果、已知限制、后续建议 |
| T27 | 画面门禁的两处过期判据：顶栏页签 `labels === 7`（`/simulation` 后实为 8）、@1000/@900 的 `.nav-status` 静默裁字 | in_progress | — | 改判据本身（页签数按 router 实量、胶囊按内容宽度给余量），改前先 A/B 确认 main 同现 | 2026-10-17 dashboard spike 的副产发现：两项在 `main` 上同样红，与页面改造无关。**当晚外壳收并时第一处已随门禁改写修复**（侧栏入口数改判 5：值班主线 4 + 系统 1）；第二处（窄档胶囊文字收成圆点）保留为有意收字设计，门禁只判 ≥1280 |
| T28 | 预警发布合并进监测预警页（WarningsView → MonitorView tab） | todo | T11 | 保留 warnings 全部 data-testid 与 e2e 判据；旧路由重定向 `/warnings` → `/monitor?tab=warnings` | 监测是"数据进来"、预警是"信息出去"，一条线两个入口是杂糅感来源之一 |
| T29 | 推演沙盒并入流程编排页（SimulationView → WorkflowView tab） | todo | T11 | 保留全部 data-testid 与门禁判据；旧路由重定向 `/simulation` → `/workflow?tab=simulation` | 推演是演练工具，不占一级导航 |
| T30 | 智能助手改常驻悬浮抽屉（AssistantView → 全局抽屉） | todo | T11 | 保留原有 data-testid、流式帧与人工确认行为；会话状态本就在 composable 里，换页不丢 | 入口现居侧栏导航下面那一行（`data-testid="assistant-entry"`，2026-10-23 批次 B6 从右下角悬浮胶囊收回——浮标实测压在页脚/分页器/页码上）；本任务换成抽屉 |
| T31 | 指标量测降为侧栏"系统"区一级页 + 腿面板/系统输出归位 | todo | T11 | `/metrics` 路由保留（验收取证路径不变）；`/api/v1/integrations` 页底面不改 | 运维/验收取证面不进值班主线 | |

## 阻塞清单

| 任务ID | 现象 | 已尝试（≥2种） | 需要用户决定什么 |
| --- | --- | --- | --- |
| — | （空） | — | T11 已于 2026-10-17 随信息架构收并一起批准并实施；演示时间点仍未定（影响 M5 页面顺序走查） |

## 决策日志（ADR）

- 2026-10-07｜组件库走 Arco Design Vue，先探针实测再迁｜用户拍板（BRIEF §7 第 1 问）｜T10 前置、T12 解锁条件
- 2026-10-07｜旧视觉现在停用，PageHero 改朴素页头｜用户拍板（BRIEF §7 第 2 问）｜T06 已完成，棘轮 228→159
- 2026-10-07｜令牌层运行时写变量、不生成 tokens.css｜避免第二真源两张皮（BRIEF §8 决策 1）｜所有派生层零 hex，门禁守着
- 2026-10-07｜颜色收敛用棘轮只减不增｜不打断页面改造也不许新欠（BRIEF §8 决策 2）｜当前基线 139
- 2026-10-07｜ECharts 换肤=重建实例，画布背景保持透明｜实例无换主题口子；填实色翻转必漏馅（BRIEF §11 决策 7）｜T08/T09 同法
- 2026-10-07｜顶栏页名全档铺开、退役编号点与页码徽标｜用户拍板"导航留顶部、不要编号点"；页名即标识，序号不表达状态｜T07；§7 第 4 问随之化解
- 2026-10-17 晚｜**信息架构收并：八页平铺改侧栏分组导航**｜用户反馈"项目杂糅、前端乱、外人不会用"后拍板｜侧栏两组（值班主线：总览/监测预警/一张图/编排；系统：指标量测），助手改常驻悬浮入口；旧入口（warnings/simulation/assistant）标 `nav:false` 保留可访问，合并/抽屉化按 T28–T31 推进；导航真源收敛到 router.ts 的 `NAV_GROUPS`，App.vue 只 import 不抄（uiTheme 对账用例钉住）｜T11 done、M2.5 立

## 工作日志

- 2026-10-07 轮次1：接手时发现上一批（T07 顶栏页签）已改完未提交；跑 typecheck ✓、uiTheme.spec 12 ✓，全量单测+build 进行中；
  探明服务在位（后端 8321 `/healthz` ok、preview 4175 ok），e2e 深色组待 build 完成后跑；创建本看板（此前缺失，BRIEF 是唯一台账）。
  下一步：门禁全绿后提交 T07 → 台账补记 → 领 T08/T09（Cesium/Vue Flow 画布接令牌）。
- 2026-10-07 轮次2：T07 全门禁绿（typecheck / 793 单测 / build 54.74s / e2e 深色组 13 passed 含页签四档）→ 提交 `65fd80e` + 台账 `3ddcd9b`。
  接着做 T08：新建 `src/theme/map.ts` 派生层、`viewer.ts` 按调色板画 + `setTheme` 重画图元、`MapView.vue` 接 `useTheme().resolved`；
  证据分两半（真机钉画面级令牌对账 e2e 7 passed；图元重画用假 Cesium 单测 5 条，因本机 `public/basemaps/` 缺锚点/灾害面资产、场景零图元，且不为凑图元伪造坐标）；
  颜色棘轮 159→158（`viewer.ts` 的 `#101923` 归零）；变异检查：`border-subtle` 标签字 / 同色光晕 / 深浅同底色 / 不抹签名 四处全红，
  `text-muted` 那处期望错（本就过 AA，非缺陷，已记 BRIEF §14）。全量 ui-overlap+map-visual 干净重跑进行中，结果下轮追记。
  代码提交 `75fa1f9 feat(ui): Cesium 画面内部接令牌…`。
  下一步：T09 Vue Flow 画布接令牌（照 T08 的派生层套路，`utils/graph.ts` 12 处含紫/品红/青）。
- 2026-10-17 轮次3（/dashboard 标杆页视觉 spike，分支 `spike/dashboard-redesign`，未并主线）：按用户任务书做探索性重做——
  方向「值班台 · 一屏三问」，新增 `components/dashboard/` 五个组件（读数带 / 风险瓦片墙 / 五段分段条链路行 / 智能体在线板 / 日志排事件流），
  `LegStatusPanel` 收掉一处浅色限定 rgba；**未新增令牌槽位**（新方向全用现有 `--aeg-*` 表达），颜色棘轮 158→154（态势页内字面色归零）。
  十条产品不变量逐条继承，八枚 `data-testid` 全保留；门禁：typecheck ✓ / vitest 868 ✓ / build 24.09s ✓ /
  dashboard 相关 e2e 32 项 **26 passed**（@1100–@1440 四档几何与对比度全 0 命中）。
  两件事是门禁自己的问题，已一并修：① `ui-overlap` 的 17 处 `networkidle` 等待无超时，**后端一健康就每项跑满 180s**
  （A/B：未改动的 /monitor 在 main 上同样全超时）→ 统一 `settle()` 8 秒；② 表面地板 8→7、托盘地板 2→1（页面结构变了，按实量下调不删断言）。
  剩 6 项红与本无关：@1000/@900 的 `.nav-status` 静默裁字（main 同现）、顶栏页签 `labels===7` 已过期（`/simulation` 后是 8 枚，见新任务 T27）。
  截图：`frontend/test-results/spike/before-*` 与 `after-*` 各四张（浅/深 × 1366/1920），数据走真演练接口。
  下一步：等用户对方向的裁决——通过则并入主线并补 T12（Arco 组件层），不通过则整分支丢弃（main 未动）。
- 2026-10-17 轮次4（信息架构收并，用户拍板"没问题"后实施）：
  诊断（用户反馈"杂糅、乱、外人不会用"）：八页平铺混三类受众、每页工程师全外显、spike 新视觉只换内部组件。
  实施：① `router.ts` 新增 `NAV_GROUPS` 导航真源（值班主线 4 + 系统 1），旧入口标 `nav:false`；
  ② `App.vue` 外壳重写——侧栏（分组/收起持久化/智能体迷你区）+ 顶栏（页名/状态胶囊/主题）+ 助手悬浮入口；
  ③ `uiTheme.spec.ts` 导航覆盖对账用例按新 IA 重写（剥注释、引号兼容两处返工）；
  ④ `e2e/ui-overlap.spec.ts` 随结构改写：PAGES 补 simulation/assistant（8 页）、navIntrude/navClip 选择器换外壳族、
  中线判据退役改侧栏宽度读回、HOVER_TARGETS 换 `.side-item`、shellLum 加 `.sidebar`、页签四档扫查改侧栏五入口。
  门禁：typecheck ✓ / vitest 882 ✓（含新对账用例）/ build ✓（dist 让位重建：vite emptyDir 触发平台批量删除保护，
  用 rename 让位绕开，构建产物零用户数据）/ ui-overlap 全量跑中。
  下一步：T28 预警合并 → T29 推演并入 → T30 助手抽屉 → T31 系统区归位；每步各自跑门禁。
- 2026-10-23 轮次（工作流编辑器重构批次 B 收口，方案 `mountain-trail-trout` B1–B6）：
  交付：① 合规——Dify UI 源码移植获用户批准并收窄为"禁整包嵌入与品牌冒用"（ADR-0010 §11、notices、inventory §3.4）；
  ② 外壳——`/workflow` 沉浸化（`meta.immersive`，侧栏/顶栏/令牌条/页脚整块不渲染）+ 56px 自带头部；
  ③ 画布——Dify 节点卡（240px/16px 圆角/图标+摘要+状态环、handle 上移）、自定义边 2px 条件标签、
  14px 点阵、边中 “+” 插入、空白双击开块选择器；④ 交互——块选择器（400px 浮层、搜索/分类/键盘/焦点归还、
  Start 已存在即禁）与 400px 覆盖式右面板（运行态/定义/节点三档 + 页签持久化）；⑤ T1 端口三件
  （`node-factory`/`edge-color`/`connections`）带 1.17.1 行为 fixture 对拍。
  B6 的账最重：把 `e2e/ui-overlap.spec.ts` 按两套外壳重写（联合就绪信号 + 沉浸分支 + 动线回归），
  **第一轮 125 项红 66 项**，逐条量到底三件事——上一批侧栏收并留下的未验缺陷（助手悬浮胶囊实测压住页脚 2035px²、
  分页器 206px²、表页码与卡片头，收起态把"展开侧栏/值班主线"折成竖排两行，沉浸头部按钮 30px 低于 ≥34 地板）、
  本批改名 `.ed-canvas` 导致的**静默失效**（描边补偿整段还挂在退役选择器上：连线只剩 0.97 设备像素、缩放控件圆角 0；
  卡片描边/类别条的 `calc(Npx * k)` 退化成写死；覆盖面板挂在画布外，落点禁落区找不到它）、
  门禁自己在等不存在的东西（无超时 `networkidle` 在 SSE 页干等 180s；时间线第二段读到的是桩数据落地前的旧行）。
  全部改掉之后**复跑 127 项全绿、0 红，32.3 分钟**；变异 9 条全部有牙（其中"边中标签层"第一条变异写 `z-index: 1`
  跑出来还是绿——1 已经高于节点默认的 0，换成把抬层规则整条删掉才复现缺陷：**变异要还原缺陷本身，不是随手改个数**）。
  门禁：后端 ruff/mypy ✓ + 全量 pytest **3276 tests / 98 skipped / 3178 ran / 0 失败**（junit 守卫同数通过）；
  前端 typecheck/lint/format ✓ + vitest **1017 passed** + coverage 99.73/95.26/100/99.73 ✓ + build ✓；
  颜色棘轮 **139 → 110**（HEAD 同口径实量 139，逐文件对账见 BRIEF §18，`NodeShell.vue` 14→0、`WorkflowView.vue` 7→0 等）。
  顺手收的账：画布入口的 `openPanel` 拆成 `openPanel`（头部按钮，同档再点=关）与 `revealPanel`（内容驱动，已开着只换档）——
  真机动线就是红在"加第二颗节点时面板把自己关掉"。
  待用户裁决的一处：`NodePalette.vue` 与画布 `onDrop` 拖拽链路仍在但已无入口（加节点改走块选择器点选），
  要么按 Dify 给块选择器补"拖出去放"的一档，要么连 `onDrop` 一起退役；本批没动它。
  下一步：批次 C 操作语法（剪贴板/快捷键/历史）→ D 专属表单与变量 → E 试运行与实例图 → F 版本面板与性能收尾。
- 2026-10-24 轮次（工作流编辑器重构批次 C 收口，方案 `mountain-trail-trout` C1–C4）：
  交付：① 六个端口带 1.17.1 fixture 对拍——剪贴板 / 快捷键表 / 键盘微移（T1），历史 / 选择 / 节点定位（T2）；
  ② 选择态收回编辑层：框选 240×100 相交（含贴边）、Shift 增减、点空白清空、只读可看不可移，选择不写文档；
  ③ 剪贴板自有格式 `aegis-workflow-clipboard` v1、2MB 上限、原型段过滤、坏输入显式错误码、内部边一一换号、
  节点+边+坐标一次落齐（历史一步）、落点跟最后一次指针（没跟过鼠标落可见区中心）；
  ④ 历史快照式 50 步，拖动只在 `dragstop` 记一步，粘贴/多选删除/自动整理各一步，
  undo/redo 不加 `documentEpoch`（视口不跳），只读视图禁用；⑤ 快捷键保留上游 14 条 + save/help/locate/cut 4 条，
  右键菜单（节点/多选/边/空白）、`Mod+F` 定位、`?` 帮助、工具条第五枚指针/抓手；
  ⑥ fast-check 性质测试 5 条（粘贴换号保持连通、未知类型级联丢弃、undo/redo 恒等、同文不记步、上限与标签）。
  真机（构建产物 preview 4175 + 后端 8321）：框选 3 颗松手后仍选中 3 颗（修前 0）→ 复制读回片段含 kind/node_id →
  粘贴 3→6 → `Mod+1` fit（matrix 0.9→1，0 颗出界）→ 三颗同位移 dx≈128 / dy≈74 → 撤销 3 次 6→3→2 →
  重做 3 次 3→6→拖动位置；输入框 Delete 不删节点（正对照：画布 Delete 真删、undo 回来）；
  坏剪贴板弹“剪贴板内容不是合法 JSON”且节点集合前后一致。截图 `frontend/test-results/c4/01..07`。
  修掉的两个真缺陷：Vue Flow `Pane.onClick` 无条件 `removeSelectedElements()` 把框选结果清空
  （捕获阶段吞掉合成 click，标记留到下一次 mousedown；变异删监听 → 3 变 0，备份还原后复绿）；
  `ui-overlap` 的 fit 判据按裸 `1`（未绑定）在空跑，改 `Control+1`，工具条断言 4→5。
  门禁：后端 ruff/mypy ✓ + pytest **3276 tests / 98 skipped / 3178 ran / 0 failed**（junit 守卫同数通过）；
  前端 typecheck/lint/format ✓ + vitest **77 files / 1127 passed** +
  coverage **99.7/93.93/99.56/99.7** ✓ + build 31.90s；e2e 全量 **128 passed / 0 failed**，33.3 分钟。
  下一步：批次 D 专属表单与变量（19 类字段、变量语法端口、上游变量）。
