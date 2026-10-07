# AEGIS 自主改进看板

> 唯一进度事实来源。每轮开工先读本文件 + `docs/UI_UX_REDESIGN_BRIEF.md`（台账/ADR 细节）+ `docs/REPORT.md` + `git status`。
> 选型结论与四问口径见 BRIEF §2/§7：组件库已批 **Arco Design Vue**（先探针实测再迁）；旧视觉已批现在停用；
> 布局骨架侧栏化与演示时间点仍未批。

## 里程碑

| 里程碑 | 状态(todo/in_progress/done) | 说明 |
| --- | --- | --- |
| M0 现状审计与选型提案 | done | BRIEF §1–§3：基线实测、耦合面量化、六维对比、用户拍板 Arco |
| M1 设计令牌与双主题引擎 | in_progress | 令牌四层/切换器/ECharts 换肤/旧横幅停用已落地；欠 Cesium+Vue Flow 画布、布局骨架（待批） |
| M2 Dashboard 标杆页 | todo | 风格标杆页按新设计重做（含三态、五段链路、七腿三态面板） |
| M3 逐页推开 monitor/warnings/map/workflow/metrics/assistant | todo | 每页三态 + 双主题截图 |
| M4 多智能体预留补齐 | todo | 全局状态区、人在回路组件、AgentMessage v1 类型/store、画布节点区分+图例 |
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
| T09 | Vue Flow 画布内部接令牌 | in_progress | T05 | graph.spec 单测 + workflow 页双主题真机 | utils/graph.ts NODE_STATE_STYLES 12 处（含原任务 #35）；同 T08 的派生层套路 |
| T10 | Arco 探针实测（暗色纯 CSS 变量？/表格 1366×768/体积变化） | todo | — | 临时分支装 @arco-design/web-vue 起探针页，三问各有实测数字 | 选型已批可装依赖；不过则退 Naive→退保留 antd |
| T11 | 布局骨架：可折叠侧栏（含智能体状态迷你区）+ 顶栏 + 栅格 | blocked | 用户批准 | — | 方向性改动，按升级机制等批准（见阻塞清单 B1） |
| T12 | 阶段 2 组件层替换 antd→Arco（16 文件/202 标签）+ e2e 51 处 ant-* 选择器迁移 | todo | T10 | 全门禁绿 + 全量 e2e | 机械可改；不得动契约 |
| T13 | uiTheme.spec.ts 按新规范重写（4 条改口径、7 条保留） | todo | T12 | 重写后 11+ 条全绿、逐条变异检查 | 改口径不是删断言 |
| T14 | /dashboard 标杆页重做（KPI/五段链路/风险网格/SSE 流/智能体组/七腿三态 + 三态） | todo | T12 | 页面级 e2e + 双主题截图 + 全门禁 | M2 交付物 |
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
| T26 | 最终验收：DoD 七条逐条勾选留证 + 全门禁 + 生产构建 + 《最终验收报告》 | todo | 全部 | 报告含命令与结果、已知限制、后续建议 | |

## 阻塞清单

| 任务ID | 现象 | 已尝试（≥2种） | 需要用户决定什么 |
| --- | --- | --- | --- |
| T11 | 布局骨架侧栏化属方向性大改，按第 6 节须先批准 | ① BRIEF §7 第 4 问已提出并给过顶栏内方案；② T07 顶栏页签全档铺名落地后"必须挪侧栏"的空间理由已消失 | 是否仍要可折叠侧栏（含智能体状态迷你区）？还是顶栏导航+侧栏仅作智能体状态区？另：验收演示时间点（影响页面顺序） |

## 决策日志（ADR）

- 2026-10-07｜组件库走 Arco Design Vue，先探针实测再迁｜用户拍板（BRIEF §7 第 1 问）｜T10 前置、T12 解锁条件
- 2026-10-07｜旧视觉现在停用，PageHero 改朴素页头｜用户拍板（BRIEF §7 第 2 问）｜T06 已完成，棘轮 228→159
- 2026-10-07｜令牌层运行时写变量、不生成 tokens.css｜避免第二真源两张皮（BRIEF §8 决策 1）｜所有派生层零 hex，门禁守着
- 2026-10-07｜颜色收敛用棘轮只减不增｜不打断页面改造也不许新欠（BRIEF §8 决策 2）｜当前基线 159
- 2026-10-07｜ECharts 换肤=重建实例，画布背景保持透明｜实例无换主题口子；填实色翻转必漏馅（BRIEF §11 决策 7）｜T08/T09 同法
- 2026-10-07｜顶栏页名全档铺开、退役编号点与页码徽标｜用户拍板"导航留顶部、不要编号点"；页名即标识，序号不表达状态｜T07；§7 第 4 问随之化解

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
