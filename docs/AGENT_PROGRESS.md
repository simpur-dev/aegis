# AEGIS 自主改进看板

> 唯一进度事实来源。每轮开工先读本文件 + `docs/UI_UX_REDESIGN_BRIEF.md`（历史台账，仍在主目录）+ `docs/REPORT.md` + `git status`；
> 一次性任务书与立项前调研已于 2026-11-04 移入 `docs/archive/`，不是现行材料（归档判据见 `README.md` 文档地图）。
> 选型结论与四问口径见 BRIEF §2/§7：组件库已批 **Arco Design Vue**（先探针实测再迁）；旧视觉已批现在停用；
> 布局骨架 2026-10-17 拍板并实施为可折叠侧栏，**2026-10-30 已整块退役**，改透明顶栏横排（见下方"现状校准"与决策日志）。

## 现状校准（2026-11-12 更新，读台账前先看）

> 本看板的"里程碑/任务表"停在早期批次，**下文历史工作日志与决策记录保留原样不改**；
> 下表以当前代码为准，与它冲突的条目按此读。

| 看板条目 | 台账写的 | 当前代码 |
| --- | --- | --- |
| M2.5 / T11 / T28–T31 | 侧栏分组导航已落地，后续任务以侧栏为前提 | `46bf57a`（10-30）移除侧栏、改透明顶栏横排；`NAV_GROUPS` 分组数据仍是导航真源，只是渲染成横排。这些任务的前提失效，**需重判，不要按旧描述开工** |
| T30 助手抽屉 | 待做，入口在侧栏导航下面一行 | `833fed9`（11-01）已落地：全站右下悬浮图标 + 右侧抽屉（打开时主内容让位、不覆盖）；`608e8e8`（11-02）补 320–480 可拖宽度、持久化与键盘调节 |
| T17 一张图 | 待做，保持零 Ion/谷歌依赖 | `7c2a8a4`（10-30）已改全屏地图 + 左右悬浮面板；离线档不依赖 Ion/谷歌，高德在线档 10-17 已接 |
| T14 /dashboard | 待 T12 组件层收口 | 10-25 `ae32311` 玻璃拟态与 KPI 四球、11-02 `608e8e8` 两行 63/37 对齐已重做；T12（antd→Arco）仍 todo |
| T27 过期判据 | in_progress | `eb6f1de`（11-01）已删掉随外壳退役的结构判据（127→122）；当前挂账是另 4 项，见下 |
| M4 智能体状态区 | 已随侧栏落地迷你区 | 迷你区随侧栏一起退役；顶栏状态胶囊与总览的智能体在线板仍在 |
| 智能助手 P0–P4 | 曾写 in_progress | **已全部完成**（11-04～11-08）：席位/通知/审计/write 动作/强认证/外部通知闭环，见决策日志 11-04～11-08 各条与 `docs/智能助手_设计文档.md` |
| L3 智能体层 | 平台五段 + 外部实现两套 | **2026-11-11 已统一**（`docs/adr/0016`）：11 本体同挂一条总线、能力矩阵单一真源、`AEGIS_AGENT_IMPL` 已删；灾种六体住在 `agents/hazards/`（原 `agents_ext/`） |
| LLM 通路 | 平台网关与外部实现各一套 | **2026-11-11 已统一**（`docs/adr/0017`）：熔断/提示词注册表（21 条）/台账/meta 在 `agents/core/` 共用一份实现，只留厂商 quirks；灾种调用已进共享台账并在治理出口合并统计 |

**当前门禁（2026-11-12 实测，junit 计数）**

- 后端：`pytest tests/unit tests/api tests/contract tests/e2e tests/integration`
  **4300 passed / 0 failed / 119 skipped**（391.8 s；`backend/tests` 213 个 `test_*.py`）；
  `ruff check` + `ruff format --check` 全绿、`mypy src` 360 文件全绿。
- 前端：`vitest` 最近一次全量 **87 files / 1278 passed**（11-08 入档）、typecheck 0 error；
  颜色棘轮基线现役 **106**（`frontend/src/uiTheme.spec.ts` 的 `COLOR_BASELINE`），正文里的 154 / 139 / 104
  都是历史实量值，别当现行基线读。

**未清项（截至 2026-11-12，这些账仍在）**

- e2e 界面自检 122 项：11-01 `0e1b31e` 按实测对比度修完字色后仍留 4 项——地图后端不可达时浮层互压 2、
  深色换肤读不到半透明卡面 1、monitor 图表换肤取样下限 1；口径未定前先留着。
- `/api/v1/agents` 的 `online`/`items` 口径不一致（README §2.3 第 8 条）仍待复测。
- `store` 读视图 **已于 P0 接上启动回填**（`read_backfill_limit`，默认 500 条 warnings/tasks；
  `chains` 不回填、`chain.list` 按需查库），README 那条已同步改正；**仍欠**超出窗口的历史查询体验。
- 灾种 LLM 仍**不走**平台网关（`docs/adr/0017` 第 1 项后续）：不进平台的提示注入围栏、不走网关的
  模型路由；但**已进共享台账**，用量/首过率/时延在治理出口与网关那本合并统计。
- `AEGIS_EXT_*` 旧名兼容分支仍在（第 2 项后续）：四处兜底都已记 deprecation 日志，
  删不删按"部署面迁移确认 + 一个发布周期内无告警"的证据走，不按时间走。

## 里程碑

| 里程碑 | 状态(todo/in_progress/done) | 说明 |
| --- | --- | --- |
| M0 现状审计与选型提案 | done | BRIEF §1–§3：基线实测、耦合面量化、六维对比、用户拍板 Arco |
| M1 设计令牌与双主题引擎 | in_progress | 令牌四层/切换器/ECharts 换肤/旧横幅停用已落地；欠 Cesium+Vue Flow 画布收尾（T09）、布局骨架曾随 M2.5 实施（该外壳 10-30 已退役） |
| M2 Dashboard 标杆页 | in_progress | 探索性 spike 已出标杆视觉并**已并入主线**（2026-10-17，`6c4b942..4f181a4` + merge `622bb92`）；10-25～11-02 又按玻璃拟态 / KPI 四球 / 63:37 两行重做过两轮，组件层迁移（T12）仍待做 |
| M2.5 信息架构收并（2026-10-17 新增） | **已反转（前提失效）** | 10-17 拍板改侧栏分组导航、外壳重构落地（T11）；**10-30 `46bf57a` 把侧栏整块移除，改透明顶栏横排**。分组数据仍在 `NAV_GROUPS`，但 T28–T31 以侧栏为前提的描述需重判，详见"现状校准" |
| M3 逐页推开 monitor/warnings/map/workflow/metrics/assistant | todo | 每页三态 + 双主题截图；范围按顶栏 5 页 + 助手抽屉的当前清单调整（不再按侧栏分组） |
| M4 多智能体预留补齐 | todo | 全局状态区（侧栏迷你区已随侧栏退役，现存顶栏状态胶囊 + 总览智能体在线板）、人在回路组件、AgentMessage v1 类型/store、画布节点区分+图例 |
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
| T11 | 布局骨架：可折叠侧栏（含智能体状态迷你区）+ 顶栏 + 栅格 | done | 用户批准（2026-10-17） | 已实施：侧栏分组导航（值班主线 4 + 系统 1）+ 顶栏（页名/状态/主题）+ 智能体迷你区 + 收起持久化；门禁已随结构改写 | 外壳重构与路由收并一次性落地，详见决策日志 2026-10-17 晚；**该外壳 10-30 已随 `46bf57a` 退役**，当前形态见"现状校准" |
| T12 | 阶段 2 组件层替换 antd→Arco（16 文件/202 标签）+ e2e 51 处 ant-* 选择器迁移 | todo | T10 | 全门禁绿 + 全量 e2e | 机械可改；不得动契约 |
| T13 | uiTheme.spec.ts 按新规范重写（4 条改口径、7 条保留） | todo | T12 | 重写后 11+ 条全绿、逐条变异检查 | 改口径不是删断言 |
| T14 | /dashboard 标杆页重做（KPI/五段链路/风险网格/SSE 流/智能体组/七腿三态 + 三态） | in_progress | T12 | 页面级 e2e + 双主题截图 + 全门禁 | M2 交付物。**spike 已并入主线**（2026-10-17，merge `622bb92`，BRIEF §15）：读数带/风险瓦片墙/五段分段条/智能体在线板/日志排事件流；组件层迁移（T12）仍待做 |
| T15 | /monitor 重做（遥测表+曲线、缺口标记、演练入口、人工上报向导+低置信核签） | todo | T14 | 同上 | |
| T16 | /warnings 重做（列表/详情、双语 translation_pending 如实标、回执、关联任务） | todo | T14 | 同上 | 不伪造藏文 |
| T17 | /map 重做（全屏 Cesium+浮动面板+详情抽屉，缺瓦片如实降级） | todo（10-30 已部分落地） | T08、T14 | 同上 + 地图两道门禁 | 离线档零 Ion/谷歌依赖，高德在线档 10-17 已接；`7c2a8a4` 已做全屏地图 + 左右悬浮面板，详情抽屉与两道地图门禁未单独复跑 |
| T18 | /workflow 重做（16 类节点、版本化定义/实例、人工决策/改参/旁路、连线校验保留） | todo | T09、T14 | 同上 | |
| T19 | /metrics 重做（P50/P95/max vs 阈值、越限高亮、未测得如实显示） | todo | T14 | 同上 | 数字只来自埋点 |
| T20 | /assistant 重做（四类任务、流式帧、动作确认卡、未配 LLM 如实显示） | todo | T14 | 同上 | 越权拒绝并留痕 |
| T21 | 全局智能体状态区（五智能体在线/工作中/降级/离线常驻，SSE+integrations 数据源） | todo | T11 或顶栏落位决策 | 真机：降级时如实显示、绝不伪装在线 | M4 |
| T22 | 人在回路可复用组件（确认/核签队列、批准/驳回/改参） | todo | T14 | 组件单测 + monitor/assistant 两处接入真机 | 为智能体输出留插槽 |
| T23 | AgentMessage v1 TS 类型 + store（按 contracts/agent_message.v1.schema.json 对账） | todo | — | 类型单测 + 与 schema 对账脚本/用例 | 不改契约本身 |
| T24 | 五段链路可视化（decided_by/耗时/产出/降级原因如实标注） | todo | T14、T23 | dashboard 真机 + 降级场景注入验证"平台降级接管"字样 | |
| T25 | 分辨率走查（1366×768 与 1920×1080 两档全页）+ 关键路径 3 步可达核验 | todo | T14–T20 | 两档全量 e2e + 截图走查 | M5 |
| T26 | 最终验收：DoD 七条逐条勾选留证 + 全门禁 + 生产构建 + 《最终验收报告》 | todo | 全部 | 报告含命令与结果、已知限制、后续建议 |
| T27 | 画面门禁的两处过期判据：顶栏页签 `labels === 7`（`/simulation` 后实为 8）、@1000/@900 的 `.nav-status` 静默裁字 | **已收口（11-01）** | — | 改判据本身（页签数按 router 实量、胶囊按内容宽度给余量），改前先 A/B 确认 main 同现 | 原两处已在 10-17 外壳改写与"有意收字"口径下消化；`eb6f1de`（11-01）进一步删掉随侧栏退役的结构判据 **127→122**。当前挂账换成另 4 项（见"现状校准"），不要再按本行描述去找那两条 |
| T28 | 预警发布合并进监测预警页（WarningsView → MonitorView tab） | todo（前提需重判） | T11（侧栏方案已退役） | 保留 warnings 全部 data-testid 与 e2e 判据；旧路由重定向 `/warnings` → `/monitor?tab=warnings` | 合并本身未做，旧入口现为 `nav:false` 深链；监测是"数据进来"、预警是"信息出去"，一条线两个入口是杂糅感来源之一 |
| T29 | 推演沙盒并入流程编排页（SimulationView → WorkflowView tab） | todo（前提需重判） | T11（侧栏方案已退役） | 保留全部 data-testid 与门禁判据；旧路由重定向 `/simulation` → `/workflow?tab=simulation` | 合并本身未做，旧入口现为 `nav:false` 深链；推演是演练工具，不占一级导航 |
| T30 | 智能助手改常驻悬浮抽屉（AssistantView → 全局抽屉） | **done（11-01 / 11-02）** | T11 | 保留原有 data-testid、流式帧与人工确认行为；会话状态本就在 composable 里，换页不丢 | `833fed9`：全站右下 56px 悬浮图标（可竖直拖拽）+ 右侧抽屉，打开时 `.main-content` 让位而非覆盖；`608e8e8`：左缘手柄拖宽 320–480、持久化 `aegis.assistant.dock`、键盘可调。入口 `data-testid="assistant-entry"` 现在顶栏导航末项，侧栏那一行已不存在 |
| T31 | 指标量测归入"系统"分组 + 腿面板/系统输出归位 | todo（前提需重判） | T11（侧栏方案已退役） | `/metrics` 路由保留（验收取证路径不变）；`/api/v1/integrations` 页底面不改 | 侧栏"系统"区已改为顶栏 `NAV_GROUPS` 的第二个分组，条目本身仍在顶栏；运维/验收取证面不进值班主线 |
| T32 | 智能助手升级：常驻抽屉 + 主动告警 + 协同处置 + 指令即执行的控制面 | **done（P0–P4 全部完成，2026-11-08 收口）** | 需求 `docs/智能助手升级方案.md` + 设计 `docs/智能助手_设计文档.md`（2026-11-04） | 按设计 §12 分 P0–P3；告警与控制相关须有演练注入脚本 | 六条决策见方案 §3；D5 = **指令即执行、助手不放大权限**。**P0 已完成**：010 六表九索引（真库建/回滚/重建）、席位（3 端点 + seed + 6 测试）、读视图回填（warnings/tasks；chains 走库）。**P1 已完成（两批）**：`services/assistant/` 拆包 + 12 动作 + 注册表自洽 + `/plan`、`/sessions/{id}` 退役 + 后端死代码 B1–B11 清零；`AssistantPanel` 抽取 + 抽屉接入 + 前端死代码 F1–F14（F8 复核保留）。**P2 第一批已完成（11-04）**：`control_actions` 持久层、`/assistant/actions` 资源化、`/confirm` 退役、执行器接台账、启动收尾卡死与过期动作。**P2 第二批已完成（11-04，告警面）**：`domain/alert.py` + `persistence/alerts.py`（UNIQUE 去重、原子迁移、UPDATE...RETURNING 升级）+ 内存 `AlertStore` 同一不变量 + `services/alerting/`；`/alerts` 四条端点（ack 幂等、transfer 需 lead、snooze 不改状态）；容器订阅 `platform.alert.>` + 升级定时任务（多副本靠 RETURNING 幂等）；广播 payload 增 `drill`（链路读数全 simulator 即演练，`stages.py::_is_drill`）；`scripts/inject_alert.py`（总线注入 / `--persist-only` 直写台账）；前端 `api/alerts.ts` + `api/operatorSeat.ts`（席位头，声明身份）+ `useAlerts`（分流去重、断线对账、Ⅰ/Ⅱ 紧急集）+ `AlertStack.vue`（在案告警卡 + 三种处置）+ App 顶部条 + 抽屉自动展开；`operators.py` 的 `_require` 提升为公共 `require_operator`（告警与席位同一鉴权口径）。门禁（11-04）：ruff 378 / mypy 189 / 后端 **3304 passed** / 前端 typecheck 0 + vitest **85 files 1265 passed**。**P1/P2 收尾已完成（11-08）**：抽 `pipeline/publisher.py::WarningPublisher`（三条发布入口收敛到 `release()`；落在 `pipeline/` 是因架构铁律禁止 `services/` import `aegis.bus`）、`011_warning_revocation.sql`（`warnings.revoked_at` 派生状态 + `assistant_notifications`，撤销不改写 `released_at`/回执）、`services/collab/tasks.py`（`handling_tasks.version` 乐观锁 + `container.on_result` 幂等建空间/任务）、`services/alerting/notifier.py`、`/notifications` 读面与 SSE 通知条；10 个 L3 控制动作（五个预警 + 五个任务/通知）与 P1 遗留的 `task.claim/ack`、`action.status` 全数落地并注册 `ActionDomain.NOTIFY`；越权闸补「整体批量指令不因动作已注册而放行」（助手只做单条处置、不放大权限）。补测：后端两组单测 42 项 + 通知 API 5 项 + 真库集成 3 项（含 011 迁移/乐观锁/撤销落库）；前端 `useAlerts.spec.ts` 加 4 项。门禁（11-08）：ruff check/format + mypy 201 文件 ✓ / 后端 unit+api+e2e+contract+integration 全绿（真库用例因本机无 PG/未设 `AEGIS_TEST_PG_DSN` 跳过）/ 前端 typecheck 0 + vitest **85 files 1269 passed**。设计文档 §3.3/§7.3/§7.6/§12 已回写代码事实（含三个不存在的 feature flag 口径修正）。**P3 三批已完成（11-08）**：七个 L4 动作与显式参数协议（`ada03d1`）、`/spaces` 协同面 + 双人复核配置项 + `audit_ops` 月分区（`ea3ad4e`）、执行器第 8 步 `audit.write` 接线（`167156a`）。**P4 已完成（11-08，设计 §14 定稿）**：① 强认证——`013_operator_keys.sql`（`operator_keys` 只存 SHA-256 + `operator_key_attempts` 按人计数）、`services/operator_auth.py`（签发/认证/二次认证/锁定；开关 `AEGIS_OPERATOR_KEY_AUTH_ENABLED` 默认关）、`X-Aegis-Operator-Key` 与 `_identity()` 单一解析点（凭证与声称不一致 → 401，不取其一）；写动作在开关打开后强制令牌（只带席位头 → `E_OPERATOR_KEY_REQUIRED`），读动作仍匿名；高危动作二次认证复用 `ActionSpec.dual_review` 那 10 个标记（与双人复核正交：横向两票 vs 纵向再出示），默认关；② 外部通知——`services/alerting/webhook.py`（白名单放行、不跟随重定向、失败不抛）、`Notifier` 改为**站内先行 → 再试外发**、通知行加 `delivery_channel/delivery_status/delivery_error/delivered_at`（`in_app`/`delivered`/`failed`，失败不回退站内那条）；③ 审计读面——`GET /api/v1/audit/ops`（operator/action/phase/space/action_id/时间 + 分页；`duty` 范围强制收成自己、`lead`/`admin` 全量；读面不写审计、不给总数）。令牌管理三条端点：签发（admin，明文只出现一次）/ 列元数据（无摘要无明文）/ 吊销（本人或 admin）。补测 47 条（认证 17 / 通知 11 / 审计 6 / 二次认证 6 / API 强认证门禁 7）。门禁（11-08）：ruff check+format ✓ / mypy 213 文件 ✓ / 后端 unit+api+contract **3400 passed / 0 failed / 0 skipped** / e2e 18 ✓。**仍未做**：账号密码/OAuth（按人令牌是部署方自管凭据）、外发重试与退避、审计导出投递 |

## 阻塞清单

| 任务ID | 现象 | 已尝试（≥2种） | 需要用户决定什么 |
| --- | --- | --- | --- |
| — | （空） | — | T11 已于 2026-10-17 随信息架构收并一起批准并实施；演示时间点仍未定（影响 M5 页面顺序走查） |

## 决策日志（ADR）

- 2026-10-07｜组件库走 Arco Design Vue，先探针实测再迁｜用户拍板（BRIEF §7 第 1 问）｜T10 前置、T12 解锁条件
- 2026-10-07｜旧视觉现在停用，PageHero 改朴素页头｜用户拍板（BRIEF §7 第 2 问）｜T06 已完成，棘轮 228→159
- 2026-10-07｜令牌层运行时写变量、不生成 tokens.css｜避免第二真源两张皮（BRIEF §8 决策 1）｜所有派生层零 hex，门禁守着
- 2026-10-07｜颜色收敛用棘轮只减不增｜不打断页面改造也不许新欠（BRIEF §8 决策 2）｜当时基线 139（现役值见 `uiTheme.spec.ts` 的 `COLOR_BASELINE`）
- 2026-10-07｜ECharts 换肤=重建实例，画布背景保持透明｜实例无换主题口子；填实色翻转必漏馅（BRIEF §11 决策 7）｜T08/T09 同法
- 2026-10-07｜顶栏页名全档铺开、退役编号点与页码徽标｜用户拍板"导航留顶部、不要编号点"；页名即标识，序号不表达状态｜T07；§7 第 4 问随之化解
- 2026-10-17 晚｜**信息架构收并：八页平铺改侧栏分组导航**｜用户反馈"项目杂糅、前端乱、外人不会用"后拍板｜侧栏两组（值班主线：总览/监测预警/一张图/编排；系统：指标量测），助手改常驻悬浮入口；旧入口（warnings/simulation/assistant）标 `nav:false` 保留可访问，合并/抽屉化按 T28–T31 推进；导航真源收敛到 router.ts 的 `NAV_GROUPS`，App.vue 只 import 不抄（uiTheme 对账用例钉住）｜T11 done、M2.5 立

- 2026-10-30｜**外壳反转：侧栏整块移除，改透明顶栏横排**｜`46bf57a`（qingyanhao）：分组导航、助手侧栏入口、智能体迷你区、收起偏好一并退役，`NAV_GROUPS` 仍作导航真源但渲染成横排；主题只留浅/深，品牌移到顶栏最右｜M2.5、T28–T31 的前提失效需重判；随外壳退役的结构判据由 `eb6f1de` 删除（127→122）
- 2026-11-01～03｜**界面重做与判据收口**：全屏地图/浮动面板、悬浮助手抽屉（可拖宽 320–480）、总览 63/37、指标页渐变柱状图、按实测对比度修字色、删退役判据｜`7c2a8a4`、`833fed9`、`608e8e8`、`1367cfc`、`0e1b31e`、`eb6f1de` 等｜本批**无 REPORT 取证入档**；`0e1b31e` 修完对比度后仍留 4 项 e2e 挂账，见"现状校准"

- 2026-11-04｜**智能助手升级立项**：常驻抽屉 + 分级主动告警 + 一条预警一个处置空间；身份先用席位声明（非强认证），P4 再升级账号｜用户明确"这个助手非常重要，必须做好"，要求方便问答、突发提醒、多人协同｜需求与代码链路见 `docs/智能助手升级方案.md`；T32 立项，同日完成 P0（迁移/席位/回填），P1 待开工

- 2026-11-04｜**智能助手不引入开源 Agent 框架**：继续自研动作注册表 + 统一执行器；借鉴 Pydantic AI 的 `ApprovalRequired`（按参数/角色条件审批）与 LangGraph 的 `interrupt` 语义，不引入依赖｜官方文档核实 + 项目能力对账（14 项卖点 13 项已有等价物）；OpenAI SDK 默认上传 trace 且含敏感数据，内网不可接受｜`docs/智能助手_Agent框架选型调研.md`；出现开放式自主协作/跨天长任务再评估

- 2026-11-08｜**P1/P2 收尾：发布链单一真源 + 越权闸收紧**｜`WarningPublisher` 必须落在 `pipeline/`，因 AST 门禁禁止 `services/` import `aegis.bus`；主链路、工作流画布、助手动作三条入口统一为 `release()`，动作层以 `PublisherPort` Protocol 反向注入｜撤销只追加 `revoked_at`，绝不改写 `released_at`/`deliveries`（否则 ≤20min 触达指标被事后操作污染）；派生状态只由 `WarningRecord.status` 决定，`rows.warning_status()` 只转发｜动作注册后越权闸不能只看"表里有没有这个名字"，整体批量指令（全网/全部/都 + 无具体编号）仍须拒绝——这是 D5「助手不放大权限」的代码落点｜设计文档 §3.3/§7.6/§12 同步回写，含三个从未存在的 feature flag 的口径修正

- 2026-11-08｜**真库集成补跑闭环：66 passed / 0 skipped**｜`42e3ed2` 提交时会话内 Docker Engine 未起、真库档按 `skipif` 跳过；本轮宿主手动启动 Docker Desktop（Engine 29.9.2）后，直接复用现成的 `aegis-postgres` 容器（PG 17.5 / PostGIS 3.5.2 / pgvector 0.8.6）与专用库 `aegis_test`，**未 build 镜像、未起新容器、未下载任何包**｜四个 PG live 档（`test_persistence_postgres` / `test_geo_endpoints_live` / `test_suggestions_live` / `test_accuracy_replay_live`）23.8s 全绿；011 迁移在真库落地（`assistant_notifications`、`warnings.revoked_at`、`handling_tasks.version`、CHECK 含 revoked、`schema_migrations` 11 行），演示库 `aegis` 仍为 16/16 未动｜取证校正：此前"沙箱内 `Start-Process` 拉起 Docker Desktop → 子进程 `wsl.exe` 被命令安全黑名单拒绝"只影响助手会话发起的进程树，不是宿主不可用；seekdb（2881）同时在运行，但它是可选混合检索索引引擎而非持久层，不能顶替 PG｜无实现问题需修，P3/P4 仍待做

- 2026-11-08｜**演示库补齐 010/011 迁移**：`aegis` 库里 010 的六表已存在但 `schema_migrations` 只记到 009（DDL 跑过却没记账），011 完全缺失｜用项目自身 `apply_migrations`（按 checksum 跳过 001–009、幂等重跑 010、执行 011），`APPLIED=010,011`、台账 11 行；跑前后 `warnings=16 / chain_runs=16 / stu=75 / operators=0 / alerts=0` 完全一致，16 条预警仍全为 `delivered`｜没有手写 DDL、没有动演示数据；`AEGIS_PG_APPLY_MIGRATIONS_ON_START=true` 原本也会在平台重启时补齐，这里提前消除 503 面

- 2026-11-08｜**P3 第一批：七个 L4 控制动作闭环**｜`workflow.start/decide/abort`、`rule.confirm_suggestion/reject_suggestion/update`、`device.control` 全数注册，新增 `ActionDomain.WORKFLOW/RULE/DEVICE`；`ThresholdSuggestionService.apply` = 写一条 pending 建议再立即 confirm，复用版本化/409/审计，不开直改路径｜沿用 `api/routers/rules.py` 铁律：阈值与版本由操作者 `key=value` 显式给出，意图层不解析建议文本、不猜数字、不替人批准，`decided_by` 取席位｜`device.control` 不复用依赖 `NodeContext` 的节点函数，改复用同一 `OutboundCaller` 白名单，避免第二条 HTTP 客户端｜架构铁律禁止 `services/` import 可选腿，规则异常的三种语义按类名在 `rule_control.py::_translate` 单点映射｜门禁：ruff check/format 0、mypy 204 文件 0、新增单测 25 项全绿、后端 unit+api+contract+e2e+integration 全绿（真库档带 `AEGIS_TEST_PG_DSN`）、前端 typecheck 0 + vitest 85 files 1269 passed｜前端首跑偶发 `EPERM`（vitest 写 Temp 缓存被宿主拦）少收集 1 文件，复跑恢复 85/1269 exit 0，与代码无关｜**未做**：空间富交互、双人复核状态机、`audit_ops` 分区（P3 第二批），外部通知与强认证（P4）

- 2026-11-08｜**P3 第二批：/spaces + HandlingBar + 双人复核 + audit_ops 月分区**｜后端新增 `api/routers/spaces.py` 五条端点（列表是设计稿之外的实施补充：界面要能选空间）＋`CollabService` 读面/广播（迁移成功即发 `handling.task.changed`，`services/` 侧以注入回调发、不 import 总线）；`expected_version` 由客户端给出，服务端不替它"读新版本再改"｜双人复核按方案 §3.1 规则 3 落成**默认关的配置项** `AEGIS_ASSISTANT_DUAL_REVIEW_ENABLED`：`ActionSpec.dual_review` 只标四类不可逆动作 10 个，`services/assistant/review.py` 管资格（复核席位取 lead 与动作门槛的更严者、一人一票、`duty` 被拒 403），第二票与 `proposed→executing` 在同一条 `UPDATE ... WHERE NOT EXISTS(同人票)` 原子完成；`confirm_action` 加票数闸，单票任何路径都绕不过｜`012_audit_partitions.sql`：audit_ops 换按 `ts` RANGE 分区（旧表改名→预建 -24～+2 月分区→搬数据→序列接续→删旧表，PK 变 `(id, ts)`），附 `aegis_audit_ensure_partitions` / `aegis_audit_detach_expired` 两个幂等维护函数由 `PostgresStore.connect()` 每次建池调用，过期月分区 DETACH 成 `audit_ops_archive_*` 不删行；前端新增 `api/spaces.ts` + `useHandlingSpace`（版本闸只前进不后退、409 对账）+ `HandlingBar.vue` 接入 AssistantPanel，App.vue 同一条共享流按 subject 分流｜~~如实记录的遗留~~ → **同日接线，见下条**｜门禁：ruff/mypy 0、单测含新增 `test_dual_review.py` 9 项与 `test_spaces_api.py` 6 项全绿、真库 integration（含 012 分区与两票原子性用例）、vue-tsc 0、vitest 87 files 1278 passed｜踩坑记录：本会话 PowerShell 里 pytest 的失败汇总行会被管道吞掉，靠 `--junitxml` 定位（教训：失败摘要必须用 junit 或独立文件重定向确认，不能只看进度点）

- 2026-11-08｜**执行器第 8 步 `audit.write` 接线：audit_ops 从"有表无写入方"到每条写动作都留痕**｜行形状唯一真源 `storage.store.AuditRecord`（服务层填、两套台账落库），写入唯一出口 `services/assistant/audit.py::AuditTrail`｜**同事务**按 §4.5 兑现：intent 行随 `create_action` 与 proposed 同一 `conn.transaction()`，result 行随 `mark_executed`/`mark_failed` 同事务，迁移命中 0 行就不写审计（两条永远成对）；幂等重放不补行｜**无确认闸的写动作**（认领/回执/关闭/单发通知）此前完全绕过台账：现在执行器在执行前补 intent、执行后补 result（关联键由审计自生成），且**没有动作台账就不执行**——不存在"跑了但没记"的路径｜**越权**：角色不足/复核资格被拒只写一行 result（`after.rejected`=错误码），匿名记 `operator_id='unknown'`（010 设计口径：该列不加 FK）；确认后执行失败、撤销成功各补对应行｜边界如实写进设计文档 §3.3/§4.9：REST 处置端点（`/spaces`、`/alerts`）**不写** `audit_ops`——人的直接操作由台账本身记人，`audit_ops` 只记经助手执行的写动作｜门禁：ruff/format 0、mypy 0（208 files）、新增 `test_audit_writer.py` 11 项（成功/失败/越权/幂等/复核拒绝/撤销/两票整链）、真库新增"审计行随迁移成对 + jsonb 往返"用例，全绿

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
- 2026-10-24 轮次（工作流编辑器重构批次 D4 收口，方案 `mountain-trail-trout` D4）：
  交付：① 两份性质测试——19 类"编辑→保存→重开"逐字段无损（编解码/store/画布三层各过一趟真 JSON），
  以及面板判据与 `validateGraph` 的关系、上游变量合成的恒等式、`{{#a.b#}}` ⇄ `['a','b']` 互转；
  ② HTTP 层留证：`upstream` 指了没连线的节点时旧 `/definitions` 回 **400** 并点名 `ghost → j` 这条缺的边，
  草稿→发布回 **422 `validation_failed`** 同一句话且不留半个版本；
  ③ 真机新增两条判据并跑通：18 类可上面板的节点逐类铺出自己的表单（字段格数 + 专属编辑器 + 可改或有空态）、
  深色 + 900px 下变量浮层可读且无元素出屏。
  性质测试把三条想当然的断言打回：面板与整图校验不是双向等价（面板多判"值形态"，已改成"超集"单向 +
  一条钉住差异的用例）；空集合/无入边的格子本来就没有输入控件（判据改成"有控件或有空态说明"）；
  窄屏按卡片边角猜拖线落点会落到别人的卡上（一律取 handle 自己的盒子，并先断言连线真的建了）。
  另有三条环境事实：fast-check v4 没有 `stringOf`、`double({noDefaultInfinity})` 不再接受该约束、
  `end` 无字段时 `constantFrom()` 直接抛；store 层测试 import 组件层会撞分层守卫。
  变异检查 3 条全有效：`decodeNodeDef` 的 config 改成 `{}` → 红 3；删面板"白名单外参数"判定 → 红 2；
  上游分组顺序整体 reverse → 红 3。外壳同批修掉一处坏法（vitest 汇总行带 ANSI 码，不剥就误判"空输出"）。
  门禁：后端 ruff/mypy ✓ + pytest **3287 tests / 0 failed / 98 skipped**（D3 时 3285，本批 +2）；
  前端 typecheck/lint/format ✓ + vitest **82 files / 1210 passed** + coverage **99.71/94/99.58/99.71** ✓ +
  build 20.16s ✓；e2e `node-forms` **4 passed**，58.9s。
  仍欠（不阻塞下一步）：全量 `ui-overlap`（需真后端 8321）本批未重跑；后端 `workflow/variables` 未落地，
  `{{#…#}}` 对服务端仍是普通文本；19 类逐类"改完→保存→F5→读回"只在模型层由性质测试覆盖。
  下一步：批次 E 试运行 + 实例图（preview/dry-run、`_previews` 隔离、实例模式画布、RunPanel 三档）。

- 2026-10-24 轮次（工作流编辑器重构批次 D1–D3 收口，方案 `mountain-trail-trout`）：
  D1 交付：后端注册表自洽对账（`NodeRegistry.audit/assert_consistent`，`WorkflowEngine.__init__` 启动即判）+
  `/node-types` 补 `category/min_config/outputs` 三格；前端 `presentation.ts` 穷尽图标与强调色、
  未知类型画虚线卡并整体只读降级、`catalog` store 的漂移对账与重试横幅。跨端契约用例改成**按括号深度扫源码**
  解析 `NodeSpec(...)`（旧正则被多行写法打断过），关键字参数先剔除再按位对齐。
  D2 交付：`utils/graph/variable.ts`（T1，`variableTransformer`/输出名归一/`isSafeVariableKey`）与
  `useUpstreamVariables`（T2，真实祖先 + 目录产出键 ∪ 上次运行多出的键 + 环上退化按 ID 排），
  与 `dify-1.17.1/variable.json` fixture 对拍。
  D3 交付：19 类字段形态全进 registry（面板字段与后端白名单**按集合相等**判，补齐 `situation_simulate.upstream`）；
  新落 `RuleEditor`/`VariableListEditor`/`StartVariablesEditor`/`NodeRefPicker`/`VariablePicker`/`TemplateField`/
  `ValidationList` + 纯判据 `configValidation.ts`：必填、上下限、算子表外、`upstream` 不是入边、
  Start 变量名形状/重名/原型链名/超 32 项/默认值与类型不符，全部长在那一格下面。
  真机（构建产物 preview 4175，目录不可达档）抓出并修掉两处：同一句必填提示摆两遍（改成编辑器只染红框、
  话由一行统一说，并加“一句话只许出现一次”的回归断言）；值班员界面残留 `model.py:61-66` 行号。
  变异检查 5 条，逐条从字节备份还原并复绿：删必填判定 → 红 2；删入边判定 → 红 2；原型链守卫换成大小写变形
  （等价于失效）→ 红 1；变量插入改成永远追加到末尾 → 红 1；Start 变量名形状改成“非空即过” → 红 1。
  外壳自身也被变异抓出一处坏法：vitest 汇总行带 ANSI 码，不剥掉就把“跑过了”读成“空输出”。
  门禁：后端 ruff check/format + mypy src ✓ + pytest **3285 tests / 0 failed / 98 skipped**（331.6s，
  junit `backend/reports/junit-d3-gate.xml`）；前端 typecheck/lint/format ✓ + vitest **80 files / 1200 passed**
  （批次内实测轨迹 1141 → 1152 → 1200）+ coverage **99.71/94.01/99.58/99.71** ✓ + build ✓；
  e2e 构建产物档 **4 passed**（`catalog-presentation` 2 + `node-forms` 2）；颜色棘轮 **110 → 106**。
  下一步：批次 D4（性质测试、19 类编辑-保存-重开无损、真机全量验收与收口）。
- 2026-11-04 轮次（文档整理，**无代码逻辑改动、未重跑门禁**）：按 git 记录把描述性文档对齐到当前代码。
  ① README「Web 页面」节：外壳改透明顶栏（侧栏 10-30 退役）、助手改全站悬浮图标 + 可拖宽抽屉，
  表格入口列、/map 全屏布局与双档底图限定、/workflow 沉浸外壳一并改写；§2.3 第 7 条行号 `:271` 按实读改 `:272`，
  第 8 条补记 `da621bf` 的处置与"尚未复测"；② 本看板加"现状校准"表、里程碑与 T27–T31 状态、两条决策日志；
  ③ `App.vue` 顶部注释从 10-17 侧栏方案重写为当前外壳，历史注释里的"中间步进器"改成"中段导航"；
  ④ `UI_UX_REDESIGN_BRIEF.md` / `REPORT.md` / ADR-0010 只加状态横幅，历史取证段落原文保留。
  **本次没有重跑任何门禁**，未清项（4 项 e2e、两个后端缺陷、批次 E 之后的 pytest 计数）原样挂账，
  也没有把任何"没测到的"写成数字。下一步：按现状校准重判 T28–T31，或先收那 4 项 e2e 红。
- 2026-11-08｜**P4（强认证 + 外部通知 + 审计读面）落地**。设计口径先落 `docs/智能助手_设计文档.md` §14（14.0 范围 / 14.1 强认证 / 14.2 外部通知 / 14.3 审计读面 / 14.4 配置 / 14.5 接口 / 14.6 不做），再按它实现。
  **① 强认证**：`013_operator_keys.sql`（`operator_keys` 只存 SHA-256、`operator_key_attempts` 按人计数且已锁定窗口不被后续失败延长）；`domain/operator_key.py`、`persistence/operator_keys.py`（读路径 `_PUBLIC_COLUMNS` 不含摘要）、`services/operator_auth.py`（签发/认证/二次认证/锁定）；`api/deps.py` 收 `OperatorKeyHeader`；`assistant_api._identity()` 是唯一解析点（令牌优先、凭证与声称不一致 → 401）；`_require_strong_identity()` 在开关打开时拦只带席位头的写动作；`AssistantService._step_up()` 复用 `spec.dual_review` 做高危二次认证（失败留审计、不消耗复核票）。
  **② 外部通知**：`services/alerting/webhook.py`（`WebhookPolicy.from_settings` + 白名单放行 + 不跟随重定向 + `deliver()` 永不抛）；`Notifier.notify()` 顺序改为站内落库 → 试外发 → `set_notification_delivery`；SQL/内存两侧通知行都加四列，广播帧带 `delivery_status/delivery_error`。
  **③ 审计读面**：`persistence/audit_ops.query()`（参数绑定、`ts DESC`、LIMIT/OFFSET）、`SupportsAuditOps` + `PlatformStore.read_audit()`（内存侧补 `id`/`ts`）、`api/routers/audit.py`（`duty` 强制收成自己）。
  **门禁**：ruff check+format ✓、mypy 213 文件 ✓、unit+api+contract **3400 passed / 0 failed / 0 skipped**、e2e **18 passed**、配置面门禁（`test_config_env_surface.py` / `test_deploy_env_contract.py`）22 项 ✓；`.env.example` 补齐 8 个 P4 键且逐个有生产读者。
  **未做**：账号密码/OAuth、外发重试与退避、审计导出投递（理由写在 §14.6）。
- 2026-11-09｜**外部交付智能体模块并入主线**（分支 `feat/agents-ext-merge`，决策记录 `docs/adr/0011`）。
  来源：`https://github.com/aaaaaelma-alt/hazard-landslide-agent`（同学的 LangGraph 多智能体实现，六 agent、约 16.7k 行、109 条用例）。
  **落位**：`backend/src/aegis/agents_ext/`（在 aegis 包内——仓库根 `agents/` 不在包路径上，代码不进 wheel/镜像）；仓库根 `agents/` 保留为交付落点**索引**（状态表已更新；该目录 2026-11-10 已移入 `docs/agents/`）；`archive/` 存两份溯源副本（外部原始物料 + 我们自己的占位 README 原貌）。
  **接口统一**：~~`agents_ext/adapter.py` 是唯一翻译点~~ → **同日更正为三层分工（契约壳 / 语义翻译 / 口径对照），见下条**——她的 `run_xxx_agent(...) -> dict` 被包成 `AgentKit.handle(AgentMessage) -> Reply`，平台侧看不见她的签名，契约门禁与总线照旧。三条口径写死在代码里并有用例钉住：数据出处标 `source=agents_ext_mock`（不冒充实测数据）、风险等级 1–5 与她的 high/medium/low 双向映射、**"没法判断"不得变成"没有风险"**（她的 status 四态逐个区分，只有 success 才产出 risk_level——这条是实现时才发现的坑）。
  **两套并存**：`AEGIS_AGENT_IMPL=builtin|ext`，默认 builtin 不变；ext 走独立可选 extra `agents_ext`（镜像与 CI 都装上，缺依赖时如实退回 builtin 不让平台起不来）。
  **代码链路审查**：逐文件比对原仓库（差异全是机械等价改动）并修了 4 处真缺陷（与 hazard_registry 逐字相同的重复函数、三个死变量、未使用循环变量、except 缺 `from exc`）+类型改造（21 处节点返回值标注、6 处 stdout.reconfigure、图构建返回类型）；mypy 对本模块**精确到错误码**豁免 7 类，不是整模块关掉。
  **安全**：原仓库把**明文智谱 API Key 写死在源码里并提交进公开仓库**——主线版本只从环境变量读；误入分支历史的那一份已重写历史清掉（分支未合入 main）。**那把 Key 必须在供应商侧轮换**。
  **可复现**：搬入/搬测试/后处理/类型修正固化为四个 Python 脚本 + `import_external_agents_all.sh` 一条命令重跑（见下条）。
  **门禁**：ruff ✓、mypy 343 文件 ✓、她的 109 条迁入用例 + 11 条适配层用例 + 265 条边界守卫全绿；CI 的 uv sync 加 `--extra agents_ext`（少它时那些用例会因缺依赖而收集失败——那该红）。
- 2026-11-09｜**外部交付智能体模块并入主线（补记：接线范围收口、三层分工、已知缺口、门禁复跑）**｜决策记录 `docs/adr/0011`，实现 `backend/src/aegis/agents_ext/`。
  **并入动作**：把一份外部交付的 LangGraph 多智能体实现（`hazard-landslide-agent`：六个 agent data / feedback / manager / risk / simulation / warning，109 条用例）搬进主线，按同一份 `AgentMessage v1` 契约接入——平台侧看不见她的函数签名（并入时经适配层转接；**收口后适配层已删除**，见文件末尾「assess 契约统一」条）。
  **落位与存档**：实现落 `backend/src/aegis/agents_ext/`（**在 `aegis` 包内**——仓库根 `agents/` 不在包路径上，代码不进 wheel/镜像，该目录保留为交付落点**索引**；2026-11-10 已移入 `docs/agents/`）；外部原始物料进 `archive/external_agents_source_20261109/`（含**已抹除密钥**的原 config.py、18MB 数据集、运行产物），我们自己的五个占位 README 原貌进 `archive/agents_delivery_placeholders_20261109/`；`archive/` 不在包路径上，不进主线构建。
  **三层分工（已被 ADR-0012 取代，2026-11-09 同日）**：`adapter.py` = AgentKit 契约壳样板（订阅/心跳/幂等/deadline/错误回执）+ 动作分派；`contract_face.py` = 契约 payload ↔ 外部调用参数、结果 ↔ 契约响应；`levels.py` = 灾种/等级/状态的口径对照。**为什么分三层**：契约壳跟着平台契约走、翻译跟着外部实现走，混在一处会让"改一条映射"需要读懂整份订阅/幂等/超时代码。**收口后三层全部删除**——`risk_agent/agent.py` 直接继承 `AgentKit` 解析 `AssessRequest`、产出 `AssessResponse`，见文件末尾「assess 契约统一」条。
  **只接线 assess.hazard**：`AEGIS_AGENT_IMPL=builtin|ext`，**默认 builtin 不变**；ext 走独立可选 extra `agents_ext`（镜像与 CI 都装，缺依赖时装配点如实退回 builtin 并记 warning）。外部实现**当前只接通了研判段的 `assess.hazard` 一个动作**；感知段（平台也发 `assess.hazard`，但要的是 `trigger_hits`）、`plan.stu`、`execute.warn`、`feedback.attribute` **明确不接线**——并入时请求被类型化错误 `E_EXT_ACTION_UNSUP` 拒绝，**收口后改为四个动作不注册、网关记 `no_agent`**（不是 error），平台走本地降级轨。**为什么不接线而不是"接了再说"**：接了回一个形状不对的载荷，平台侧会"判不合契约再回落"——看着像降级、其实是接线失败，那会把一条没通的链路记成"外部实现跑过"。
  **已知缺口（如实记，不写"已解决"；① ② 已于同日收口关闭）**：① **数据面缺口**（**已关闭，ADR-0012**）——平台研判请求给的是**规则命中**（`hits`），外部因子引擎要的是**观测事件**（含 `rainfall_mm_h` 等灾种因子字段）；适配层把 hits 转成事件骨架，因子字段缺失 → 外部按自己的门控判数据不足 → **外部研判在当前形态下交不出结论，会诚实降级回平台规则轨**（这是降级，不是接通）。收口后平台构造 `assess.v1` 证据包随请求推送（窗口观测 + 区域 `site_profiles`），并暴露 `GET /api/v1/assess/evidence` 供拉取；② 外部 risk_result **没有 `confidence`**（契约要求）→ 适配层**不产结论**（`E_EXT_NO_CONFIDENCE`），不填 0.0 冒充一条有效研判。**已关闭**：外部按确定性策略自产 `confidence` + `confidence_basis`，MOCK 封顶 0.60；③ **线程里的图取消不掉**——入口是同步函数，放在 `asyncio.to_thread` 里跑（直接调会阻塞总线），deadline 只取消协程，超时后那张图仍在跑（内部还有重试环），继续烧 CPU 与模型调用，要掐断得走进程隔离；④ **外部的 LLM 绕开平台网关**——自建 `ChatOpenAI` 直连，**不经 `services/llm/llm_gateway.py`**，即不经提示注入围栏、不进平台模型台账；⑤ **共享记忆未打通**（平台 `services/memory/` 与外部自己那份反馈记忆 JSON 是两套）；⑥ **131 处 `print()` 噪声**（37 个文件直接打 stdout，不进结构化日志、不带 trace_id）；⑦ **外部的 manager 编排能力未接入**（全局调度图与重试/返工闭环在包里但没接上，平台侧只用了它的 risk_agent）。
  **密钥轮换要求**：原仓库把**明文智谱 API Key 写死在源码里并提交进公开仓库**——主线版本只从环境变量读（`AEGIS_EXT_LLM_API_KEY`），但**那把 Key 已在原仓库公开过，必须在供应商侧轮换**：删掉不等于止损，"我们经手过它"这件事要求轮换。
  **可复现（已退役）**：搬入/搬测试/后处理/类型修正曾固化为**四个 Python 脚本**（`import_external_agents.py`、`import_external_agents_tests.py`、`postprocess_external_agents.py`、`postprocess_external_agents_types.py`）+ `import_external_agents_all.sh` 一条命令重跑；收口后这些脚本移到 `archive/external_agents_source_20261109/scripts/`——**仅保留原始交付快照，不再作为更新入口**，模块现在树维护。
  **门禁（并入时口径，复跑取数；收口后的复跑数字见文件末尾条）**：契约门禁 `tests/contract` **87 passed / 0 failed / 0 skipped**（其中新增 `test_agents_ext_conformance.py` **27 条** = 外部实现自己过的契约 7 项一致性门禁）；外部 109 条迁入用例 `tests/unit/agents_ext` **109 passed**；适配层 `tests/unit/test_agents_ext_adapter.py` **26 条**（收口后该文件已删除）；边界守卫 `tests/unit/test_agents_ext_boundaries.py` **269 条**（按文件参数化：132 文件 × 2 条 + 5 条独立判据，**条数随文件数变动**，它守的是"每个文件都过"）；全量 pytest **3849 passed / 0 failed**（口径 = unit + api + contract + e2e：3507 + 237 + 87 + 18，与此前"unit+api+contract 3400"同源，差额 431 = 外部用例 109 + 契约门禁 27 + 适配层 26 + 边界守卫 269）；**另列**：`tests/integration` 128 条里 116 条因本机无 PG（未设 `AEGIS_TEST_PG_DSN`）跳过、实跑 12 条，`tests/perf` 21 条单列——这两个目录不进上面那个口径，别把跳过数读成通过数。
- 2026-11-09｜**assess 契约统一（收口：统一 DTO、补齐观测数据面、删除适配层）**｜决策记录 `docs/adr/0012`，实现落 `backend/src/aegis/domain/assess.py`、`pipeline/assess_input.py`、`storage/site_profiles.py`、`agents_ext/risk_agent/agent.py`。
  **① 一套 DTO**：`AssessRequest` / `AssessResponse` / `AssessEvidence` / `FactorObservation`（`assess.v1`，`extra="forbid"`）+ `DataProvenance`（`domain/provenance.py`）+ `SiteProfile`（`domain/site.py`）住在 `aegis.domain`；builtin（`agents/assess.py`）、mock（`agents/mock.py`）、ext（`risk_agent/agent.py` 直接继承 `AgentKit`）三个实现共用一套接口；平台不再认识"外部结果 dict"，`stages.py` 里手写字段解析的 `_parse_agent_assessment` 已删除。
  **② 数据面补两端（推 + 拉）**：`AssessEvidenceBuilder` 把触发链读数与 `store.telemetry` 窗口查询合并（按站点/指标/时间去重、质量过滤、声明式派生表），证据包随请求推送；`GET /api/v1/assess/evidence` 与 `GET /api/v1/site-profiles[/{region_code}]` 供拉取（ext `evidence_pull.py` 用 stdlib urllib，凭据只读环境变量）；静态孕灾属性新建区域级 `site_profiles`（内存 + PG `persistence/sql/014_site_profiles.sql` + `scripts/import_site_profiles.py`，演示数据三区 15 条属性）。显式不映射的 metric 进 `unmapped_metrics` 并写原因，不近似冒充（`new_snow_cm` 不冒充 `new_snow_24h_cm`、NTU 不冒充序数 `seepage_level`）。
  **③ 删适配层**：`agents_ext/adapter.py` / `contract_face.py` / `levels.py` 与其 26 条适配层用例删除，三模块 import 必抛 `ModuleNotFoundError`；`agents_ext/runtime.py` 只注册 `assess.hazard`，其余四动作不注册 → 网关记 `no_agent`（不是 error），平台走本地降级轨；错误码统一为 `E_ASSESS_*`（旧 `E_EXT_*` 随 `levels.py` 消失）；灾种键统一为平台值，旧串只留在 `hazard_registry.HAZARD_ALIAS` 与一条注释。
  **④ confidence 由生产方自产**：`risk_agent/confidence.py` 用确定性策略 `0.15 + 0.40×coverage + 0.05×validation`，封顶 0.60，带可解释的 `confidence_basis`；MOCK 权重不因接线变真——`is_mock=True`、`human_review_suggested` 恒真。
  **⑤ 溯源透传**：`RiskVerdict` 增 `data_provenance` / `confidence_basis` / `assessment`，`as_payload()` 三键 → `chain_runs.risk` 原样落库 → API/前端；规则引擎写 `telemetry_rule`、上报解析写 `report_parse`、builtin 写 `llm`、ext 写 `factor_engine_mock`。
  **门禁（收口后实测，junit `backend/reports/full.xml`）**：全量 `uv run pytest -q` **4095 tests / 0 failed / 0 errors / 119 skipped**（498.7s；跳过全部来自 `tests/integration` 的无 PG 分支，14 条实跑；`tests/perf` 21 条单列，不进 REPORT 口径）；REPORT 同口径 unit+api+contract+e2e **3941 passed / 0 skipped**（3598 + 240 + 85 + 18）；`ruff check` + `ruff format --check` **558 文件全绿**、`mypy src` **353 文件全绿**。新增/重写用例条数：契约 DTO 18、证据构建 24、site 存储 7、导入 16、研判段 9、ext 服务 17、因子引擎（观测输入）10、置信度 8、ext 契约门禁 24、智能体契约 18、真栈 e2e 2、边界守卫 275、OpenAPI 契约 16。
  **真栈演练（内存真栈，`agent_runtime=real` + `agent_impl=ext`，simulator seed=99，LLM 断网走确定性兜底）**：`540121` → `assess=agent`、等级 3、confidence **0.52**、`basis=coverage=0.80;validation=pass;method=factor_engine_mock;cap=0.60`、provenance `factor_engine_mock` 且 `is_mock=True`、人工核签 true、零降级；`540221` 与 `540321` 如实"研判降级"到本地：P 覆盖率 0%（缺 `catchment_area_km2` / `channel_gradient`）与 T 覆盖率 50% 低于 60% 门控，本地规则结论分别 1 / 2 且 provenance=`telemetry_rule`——**没有把"算不出"写成"无风险"**。
  **验收补证**：五受支持灾种只有"合法响应 / `E_ASSESS_INSUFFICIENT`"两种结局（用例逐个钉住）；`GET /api/v1/assess/evidence` 返回体可被 `AssessEvidence.model_validate` 直接解析；演示导入 dry-run `{"status":"validated","rows":3,"attributes":15,"demo_rows":3}`；前端 `npm run typecheck` 通过；`AgentMessage` 信封与 `contracts/agent_message.v1.schema.json` 未改。
  **仍未做（不声称）**：只接 `assess.hazard`（plan/execute/feedback/perceive 无 agent）；ext 的 LLM 仍绕开平台网关（不进模型台账、不经提示注入围栏）；LangGraph 线程超时不可强杀；`manager` / `data_agent` 仍在 `run_risk_agent_legacy` 兼容轨（仅未接线 manager 使用，平台路径禁止引用，ADR-0012 记待退役）；MOCK 权重与原型公式未升级为真实预警。
  **脚本退役**：`import_external_agents*.py/sh` 与两个 postprocess 脚本移到 `archive/external_agents_source_20261109/scripts/`，仅保留原始交付快照，不再作为更新入口——模块现在树维护。

- 2026-11-10｜**外部实现 LLM 的熔断与客户端复用（把"上游坏了"从三秒等待变成毫秒级降级）**｜决策记录 `docs/adr/0013`，实现落 `backend/src/aegis/agents_ext/llm.py`、`agents_ext/llm_breaker.py`、`agents_ext/config.py`。
  **先量后改**：cProfile 量到 ext 研判单次 2952 ms 里 **26.3 s / 32.5 s（81%）** 是 openai SDK 的指数退避 `time.sleep`（`_sleep_for_retry`，0.5s/1.0s 两级），根因是 SDK 默认 `max_retries=2` 对上游 5xx 盲重试——一次逻辑调用变成三次 HTTP 往返，而节点早就有确定性兜底在等着。纯 CPU 计算不到 0.5%。
  **改了三件**：① 关掉 SDK 盲重试（`AEGIS_EXT_LLM_MAX_RETRIES`，默认 0）；② 加熔断 `agents_ext/llm_breaker.py`（纯逻辑、无 IO、可直接单测；只记"服务不可用"——连接错误/超时/5xx/429，**4xx 与语义错误不记**，否则 Key 错、提示词问题会被一段冷却期糊过去；半开只放行一次试探）；③ 客户端进程级复用（凭据用 SHA-256 指纹入键，明文 Key 不落缓存键）。
  **熔断口径有意不同于平台网关**：平台网关的熔断**没有恢复窗口**（模型失败就跳过，靠 fallback 模型接力），外部实现只有一个模型，照抄会让熔断永久失效（上游恢复了也永远用不上），所以外部这一侧自带冷却窗口（默认 60 s）。
  **最容易做错的一处**：熔断检查点在 `_generate`（client 的薄适配层）而**不在** LLM 工厂——`risk_assess_node` / `risk_validate_node` 里 `get_risk_llm()` 是在 `try` 块**之外**调用的，在工厂里抛错会冲掉节点既有的"模板兜底"契约（节点整个失败而不是降级出模板说明），那是比慢更糟的语义回归。放 `_generate` 则异常落在节点 try 内，**兜底行为逐字不变**。
  **前后对比（同一脚本、同一负载、同一机器；上游为真实智谱端点且当前返回 5xx）**：研判中位 **2952.18 ms → 4.14 ms（713×）**，吞吐 0.3 → **241.4 次/秒**。上游健康那一档也量了（`scripts/bench_assess_llm_healthy.py` 起本地假 OpenAI 兼容端点，四变体同负载）：11.08 → **9.34 ms（1.19×）**，客户端复用贡献 1.23×。
  **一处被实测推翻的假设（如实记，避免被后来的人照抄回去）**：原以为"每次新建 client → 每次新建 httpx 连接池 → 每次 TLS 握手"。**实测不是**——langchain 的 `_get_default_httpx_client` 按 `(base_url, timeout, socket_options)` 缓存并共享 httpx 客户端（两个独立 client 的 `root_client._client` 是同一对象）。复用的真实收益是**省构造开销（0.26 ms/次）+ 让熔断账能跨调用累积**，不是省握手。代码注释已按实测改正。
  **门禁（2026-11-10 本机）**：`pytest tests/unit tests/api tests/contract tests/e2e` **4023 tests / 0 failures / 0 errors / 0 skipped**（junit 计数）；`ruff check src tests scripts` 与 `ruff format --check` 本次改动 6 文件全绿；`mypy src/aegis` 355 文件全绿。新增用例 26 条（熔断状态机 11、记账口径 9 参数化、客户端复用 3、语义不变 2、端到端熔断 1）。
  **仍未做（不声称）**：ext 的 LLM 仍绕开平台网关（不进模型台账、不经提示注入围栏）——改走网关是 async 契约而 ext 是同步调用，且边界守卫禁止外部模块依赖 `aegis.services`，另立一项；熔断状态是进程内的，多副本各自计数（跨副本共享另立一项）；节点兜底逻辑本身一个字未改。

- 2026-11-10｜**后端观测与存储链路的热点消除（两轮）**｜决策记录 `docs/adr/0014`、`docs/adr/0015`，实现落 `backend/src/aegis/observability/tracer.py`、`observability/metrics.py`、`bus/gateway.py`、`storage/store.py`、`services/llm/llm_gateway.py`、`container.py`、`agents/rest_context.py`、`agents/assess.py`、`pipeline/stages.py`。
  **第一轮：汇总出口一次算完**。四处同一形状的缺陷——每次要一个汇总数就把整份底表扫一遍：时延账本 `snapshot()`（每个指标名各扫一遍，12 指标名 × 20 万样本 = **64 ms**）、增量导出 `iterate_since()`（每次全量扫，Prometheus **每 10 秒**抓一次）、Prometheus 导出器（为拿"在线智能体数"这一个整数去算整份网关快照）、两个内存存储的旁路索引（put 2 万条、上限 5000 → 索引留 **2 万**条）、`list_spaces()` 的 N+1（200 空间 × 2 万任务 = 26 ms，而它自己的文档写着"不做 N+1"——**代码与承诺不一致**）、LLM 首过率逐 task 全表过滤。
  改法：账本分桶后 O(样本数)；增量导出从尾部反向走（seq 单调 → 新样本必是尾部一段）；网关单开只读出口 `online_count`（不改 `snapshot()` 形状——它是对外契约）；存储索引与主集合同步收缩；空间计数与 LLM 用量各改一次归拢。实测：账本聚合 64.4 → 22.5 ms（2.7×）、增量导出 8.9 → 0.0 ms、空间计数 25.6 → 1.0 ms（26×）、首过率 20.8 → 8.9 ms、预警索引 20000 → 5000 条。
  **第二轮：可复用的东西要复用**。`RestAssessContext` 每次请求新建 `httpx.AsyncClient`——根因是构造要**从磁盘重载 CA 证书包**（`_ssl._SSLContext.load_verify_locations`，**11.1 ms/次**纯 CPU），而一次 `corroborate()` 发两个请求，于是每条研判链路付 22 ms 的**事件循环卡顿**（实测 20 次构造期间 5 ms 周期的业务协程被推迟 **304 ms**）。改法：门面持有懒建复用的 client（令牌带在请求上而非 client 上，换令牌不重建）、`aclose()` 由 `AssessAgent.stop()` 调用（谁建谁关）；`stages._warnings` 这份"便捷副本"加窗口（此前无上限，每条约 4 KB，十万条 150 MB）。实测：`corroborate()` **28.32 → 2.02 ms（14×）**，事件循环最大推迟 304 → 13.4 ms。
  **等价性怎么验才算真**（这批改的是"改了会得出不同的数"的东西）：不拿"新代码自己跟自己比"当证据，把**旧算法原样重写一遍**当基准，新旧输出逐项比对——`snapshot()`/`stats()`/`iterate_since()`（八种游标，含"游标比全部样本都新"与"账本滚动后游标指向已被挤掉的样本"两种边界）、空间计数、LLM 首过率逐项一致；REST 侧另钉三条：服务重启后同一个 client **自动恢复**、服务不可达仍如实记两条降级不抛异常、智能体停机必须关门面的客户端。
  **两处口径被顺手改掉的后果，如实记**：① `WarningStore.get()` / `TaskStore.get()` 对**已被淘汰**的记录从此返回 `None`（此前索引留着它，能捞到一条列表里已经不存在的记录）——这是把"索引与列表对同一批记录给出不同答案"修掉的代价，只影响内存形态；② `stages._warnings` 超出窗口的记录不再保留（此前无上限）——预警的权威读视图是 `store.warnings`（自带上限），这份只是便捷副本。
  **查过但决定不改的**：① 消息校验的"序列化→反序列化"往返（`model_dump_json` + `json.loads`）——实测只占校验耗时的 12%（0.020 / 0.172 ms），大头是 JSON Schema 校验本身（0.152 ms），**不值得动**；② MCP 取数门面的"每次取数开合会话"——那是**协议级会话**（connect/initialize/call/close），其 docstring 已写明理由，与 REST 的 keep-alive 不是一回事，**不动**；③ `AlertStore` / `ControlActionStore` / `NotificationStore`——它们**已有**配套淘汰逻辑，本批只修缺这一段的两处。
  门禁（2026-11-10，本机）：`pytest tests/unit tests/api tests/contract tests/e2e` **4063 tests / 0 failures / 0 errors / 0 skipped**（junit 计数，两轮共 +40）、`ruff check src tests scripts` 与 `ruff format --check` 全绿、`mypy src/aegis` 355 文件全绿。

- 2026-11-11～12｜**统一智能体层与 LLM 通路（把"两套系统"收成一套）**｜决策记录 `docs/adr/0016`、`docs/adr/0017`，实现落 `backend/src/aegis/agents/`（`core/` 共享内核、`hazards/` 灾种六体、`capabilities.py` 能力矩阵）。
  **① 智能体层统一（11-11）**：平台五段内环 + 灾种六体共 **11 个本体**同挂一条总线，能力与归属的唯一真源是 `agents/capabilities.py`（每项能力恰好一个主人），`AEGIS_AGENT_IMPL` 已删——不存在"用哪套实现"的开关；灾种六体随 `AEGIS_AGENT_RUNTIME=real` 与平台五段同装，闭环内部派单也走总线（进程内直调会绕过信封/deadline/幂等/错误分类）。四腿接入生产链路 + 闭环入口 `POST /api/v1/hazards/workflow`。
  **② LLM 通路统一（11-11）**：熔断（带 60s 冷却 + 半开试探）、提示词注册表（**21 条**：平台 6 + 灾种 15）、调用台账、调用元数据下沉到 `agents/core/`，平台网关与灾种 provider **共用一份实现**；只留厂商 quirks（`json_object` 修复轮 / GLM 的 `function_calling` 双通道）。灾种 15 条正文用 AST 从原 f-string 抽取并逐条渲染比对（**15/15 逐字一致**），`hazards/prompts/` 目录删除；D10 凭证补齐 8 处 `Reply()`，`trace_id_var` 此前全仓零 `.set()` 的老问题一并修掉（JSON 日志终于有 trace_id）。环境变量族改名 `AEGIS_EXT_*` → `AEGIS_HAZARD_*`，旧名兼容读取。
  **③ 台账口径与治理出口（11-12）**：落账统一了但账没人读、口径也不齐——token 恒 0（成本归因假零）、attempt 恒 0（双通道回退被读成"首答就过"，首过率系统性虚高）、"HTTP 200 但没产出合法对象"记 ok（自然语言冒充首过）。三条全部修正：token 取响应 usage（缺省退消息 `usage_metadata`，都没有才记 0）、通道二记 `attempt=1`、不合规改判 `malformed`；改判按**本次调用链**定位（多腿并发时按"全局最后一行"会改到别人的账）。两本台账（网关随实例 + 灾种进程级）在 `GET /api/v1/metrics/latency` 的治理出口合并统计。四处旧名兜底（`_env`/`_runtime_path`/数据 API URL+TOKEN）都补了 deprecation 日志——"等现网迁移完再删旧名"的前提是能观察到谁还在用。
  **门禁（2026-11-12 本机）**：`pytest tests/unit tests/api tests/contract tests/e2e tests/integration` **4300 passed / 0 failed / 119 skipped**（391.8 s）、`ruff check` + `ruff format --check` 全绿、`mypy src` **360 文件**全绿。新增门禁：熔断状态机 19 条、提示词版本 + 正文指纹 3 条、台账口径 10 条、两本台账合并 6 条、旧名可观察 4 条；`test_capability_matrix.py` 钉住"每个登记能力有真实派发点、每个派发点都已登记"。
  **仍未做（不声称）**：灾种 LLM 仍**不走**平台网关——不进平台的提示注入围栏、不走网关的模型路由（这两条是真缺口；"不进模型台账"已不成立，台账已共享）；`AEGIS_EXT_*` 兼容分支仍在，删不删按部署面证据走；prompt 语义演化从此受正文指纹门禁约束（改正文不升版本即红）。
