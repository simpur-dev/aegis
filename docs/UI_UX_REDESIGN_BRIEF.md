# AEGIS UI/UX 重做台账与组件库选型提案

> 这份文档是"持续改进模式"的**进度台账 + 决策记录（ADR 式）**。
> 新接手的 agent：先读本文 → 再读 `docs/REPORT.md`（实测与验收口径）→ `git log --oneline -8` → 从"下一步"接着做。
> 规则来源：`UI/UX 全面重做（双主题）+ 多智能体预留设计`（alwaysApply）。

---

## 0. 一句话现状

旧风格（浅蓝底 + 玻璃白卡 + 渐变描边 + ◆ 装饰，对标 NexusMind 的那一版）**已被判定为糟糕、不专业，整体推翻重做**；
本轮（2026-10-07）**没有改任何代码**，只做了两件事：量出可核对的基线、给出组件库选型提案。**选型未确认前不动组件库。**

---

## 1. 基线（2026-10-07 实测，全部有命令可复跑）

### 1.1 门禁基线（重做期间的安全网，必须保持绿）

| 命令 | 结果 |
| --- | --- |
| `npm run typecheck`（`vue-tsc --noEmit`） | 通过，无输出 |
| `npm run test`（vitest） | **769 passed / 42 个文件** |
| `npm run build` | 通过，`built in 47.87s`；`dist` 50 MB，其中 `Cesium-*.js` 4869 KB、`ui-*.js` 1385 KB、`charts-*.js` 491 KB |
| `npx playwright test e2e/ui-overlap.spec.ts` | 上一轮全量 **100 passed（21.0m）**（HEAD `7cb8bd6`） |

**阶段 0 之后的同一条门禁（2026-10-07 复跑）**：typecheck 通过；vitest **786 passed / 44 个文件**（769/42 → 新增 `theme/tokens.spec.ts` 9 条、`theme/useTheme.spec.ts` 7 条、`uiTheme.spec.ts` 的颜色棘轮 1 条：769+9+7+1=786，文件 42+2=44）；
`npm run build` **✓ built in 47.01s**；后端 `ruff check` 通过、`pytest -q` exit 0（本批零后端文件改动，只作回归确认）。
画面门禁见 §9——第一次全量跑出 6 红，根因是**门禁自己有未声明的数据前置**，已改成自种后复跑。

### 1.2 耦合面（决定迁移工作量的真实数字）

| 量 | 值 | 怎么量的 |
| --- | --- | --- |
| `src` 里 `a-*` 组件标签出现次数 | **202 处 / 24 种组件** | `grep -rhoE "<a-[a-z-]+" --include=*.vue src \| sort \| uniq -c` |
| 用量最大的几种 | tag 34、button 26、descriptions-item 23、card 21、space 13、col 13、alert 10、table 7、row 7、form-item 6、select 3+option 5、input 5、statistic 4、switch 3、modal 2、textarea 2、list 2、divider 2、timeline 1、empty 3 | 同上 |
| 真正 `import ... from 'ant-design-vue'` 的文件 | **16 个**（其中测试 2 个） | `grep -rl "ant-design-vue" src` |
| 直接 import 的具名符号 | 只有 `Modal`、`message` 两个（其余全走全局注册 + `<a-*>`） | grep |
| e2e 门禁里指向 **antd 内部类名**的选择器引用 | **51 处**（`ant-modal` 10、`ant-empty` 8、`ant-btn` 8、`ant-message` 7、`ant-card` 4、`ant-table` 2…） | `grep -o "ant-[a-z-]*" e2e/ui-overlap.spec.ts \| wc -l` |
| e2e 门禁里指向 **我们自己的类名**的引用 | **124 处**（`page-hero` 36、`fresh` 16、`wf-def` 13、`wf-node` 12、`sys-term` 10、`risk-chip` 7、`empty-hero` 5…） | grep |
| **硬编码颜色字面值** | **478 处**（`theme.css` 101、`App.vue` 59、`WorkflowView.vue` 33、`RuntimePanel.vue` 27、`PageHero.vue` 26、`SystemTerminal.vue` 22…） | grep |
| 现有 CSS 变量引用 | 只有 **32 处**，且只覆盖动效（`--motion-base` 19）与芯片/画布局部 | grep |
| 旧视觉源码门禁 | `src/uiTheme.spec.ts` **11 条**（232 行） | `grep -n "  it('" src/uiTheme.spec.ts` |

**读出来的三件事**：
1. **换库的代价主要不在组件**（16 个文件、202 个标签，机械可改），**而在 51 处 antd 内部类名断言 + 观感重做本身**；
2. **真正欠的是令牌层**：478 个字面色 vs 32 个变量引用——这与选哪个库无关，是必须先还的债；
3. 门禁的**几何/对比度/折行/动效**类判据（100 项里的大头）**与具体库无关**，重做期间它们仍然有效，是防回归的底盘。

### 1.3 候选库的公开元数据（`npm view`，2026-10-07）

| 包 | 版本 | 最近发布 | 许可证 | 解压体积 | peer vue | 类型入口 |
| --- | --- | --- | --- | --- | --- | --- |
| `ant-design-vue`（现用） | 4.2.6 | **2025-12-21** | MIT | 77.9 MB | `>=3.2.0` | 本次 `npm view types` 未回值（走 `typings`/`exports`；`vue-tsc` 全绿可证有类型） |
| `naive-ui` | 2.45.3 | 2026-08-27 | MIT | 51.9 MB | `^3.0.0` | `es/index.d.ts` |
| `element-plus` | 2.14.7 | **2026-09-30**（最活跃） | MIT | 44.0 MB | `^3.3.7` | `es/index.d.ts` |
| `@arco-design/web-vue` | 2.58.0 | 2026-04-16 | MIT | **30.3 MB**（最小） | `>=3.1.0` | `es/index.d.ts` |

> 注：这一列只证明"包在、许可可用、维护节奏如何、体积量级"。**暗色原生程度、表格能力、令牌模型**这三项是库的设计特性，
> 我目前只有文档层面的了解——**已列入下一步的验证动作**（装到临时分支跑一个双主题探针页实测），不在此处当结论写。

---

## 2. 选型对比（六维）

评分口径：★★★ 开箱即用 / ★★ 官方支持但要自己接线 / ★ 需要社区方案或自己造。

| 维度 | 保留 ant-design-vue 4 | Naive UI | Element Plus | Arco Design Vue |
| --- | --- | --- | --- | --- |
| **双主题/暗色原生** | ★★ `theme.darkAlgorithm` + ConfigProvider，能切；但组件里有大量写死的装饰（阴影/渐变），暗色下要逐个覆盖 | ★★★ `n-config-provider :theme="darkTheme"` + `themeOverrides`，主题就是一个 TS 对象 | ★★ `html.dark` + 引官方 `dark/css-vars.css`，是 CSS 变量路线但要额外引一套样式 | ★★★ 暗色就是 `body[arco-theme='dark']` 上换一组 CSS 变量——**与"所有表面读同一组变量"这条要求同构** |
| **TS 与 Vite 契合** | ★★ 装机可用，但类型入口不是 `types` 字段；样式走 cssinjs（运行时生成哈希类，见 `css-1utvnvw` 这类） | ★★★ TS 原生、无全局样式注入 | ★★★ 类型完整，样式是 SCSS + CSS 变量 | ★★★ 类型完整，按需引入 + CSS 变量 |
| **表格/表单能力**（监测页重度） | ★★★ `a-table` 7 处 + 分页/排序/固定列成熟 | ★★★ 虚拟滚动表格强 | ★★★ 生态里政务台账案例最多 | ★★ 表格够用，复杂合并/虚拟滚动不如前三者 |
| **迁移工作量**（按 §1.2 实测） | ★ 零迁移 | ★★ 16 文件 / 202 标签 + 51 处 e2e 类名断言 | ★★ 同上 | ★★ 同上 |
| **与 Cesium / Vue Flow / ECharts 共存** | ★★ 都能共存；但换肤要各写一份（ECharts 要重建 option、Cesium 要改基图/天空、Vue Flow 要改节点 CSS） | ★★ 同上 | ★★ 同上 | ★★★ 因为暗色是 CSS 变量，**Cesium 面板 / Vue Flow 节点 / ECharts 容器可以直接读同一组 `--aeg-*` 变量**，不需要三套主题代码 |
| **离目标观感的距离**（Arco Design Pro 的信息组织 + Linear 的克制） | ★ 默认观感就是"antd 味"，要对抗它 | ★★ 干净、偏 Linear | ★ 默认偏传统后台 | ★★★ **就是 Arco 那一套**，不用对抗默认样式 |
| **长期维护** | ★★ 最近一次发布 2025-12-21（≈9 个月前） | ★★★ 2026-08 | ★★★ 2026-09 | ★★ 2026-04（≈6 个月前），字节维护 |

---

## 3. 推荐（明确一句话版）

**首选 `@arco-design/web-vue`；次选 `naive-ui`；不建议 `element-plus`；"保留 antd 只做令牌+骨架"作为进度受挫时的兜底。**

理由，按重要性：

1. **目标观感就是 Arco**：规则写的对标物是 Arco Design Pro。选 Arco 意味着"少打一场把默认样式改干净的仗"——
   而旧版三十批的经验恰恰证明：**对抗组件库的默认观感，成本全花在覆盖上**（`theme.css` 521 行里 101 处字面色，大半是在覆盖 antd）。
2. **它的暗色实现方式与本项目的硬要求同构**：Arco 换肤 = 在 `body[arco-theme='dark']` 上换一组 CSS 变量；
   而规则要求"Cesium/ECharts/Vue Flow 也读同一组令牌"。CSS 变量路线让这件事变成"接一根线"，CSS-in-JS 路线（antd）则变成"每个库各写一套主题对象"。
3. **迁移面比想象小**（实测，不是感觉）：只有 16 个文件 import 过它、202 个标签、具名符号只用了 `Modal`/`message` 两个。
4. **体积最小**（30.3 MB 解压），对"离线自托管 + 评审笔记本 1366×768"友好。

**为什么不选 Element Plus**：默认观感离目标最远（传统后台味），暗色要靠引入第二套 CSS 变量文件；它的优势（生态/政务案例）在"我们要重做观感"这个前提下不加分。

**必须先做的验证（不猜）**：装到临时分支，跑一个双主题探针页，实测三件事——
① Arco 暗色是否真的只靠 `body[arco-theme]` 一组变量；② 表格在 1366×768 下固定列 + 横向滚动是否正常；③ 按需引入后 `ui-*.js` 的实际体积变化。
**验证不过就退回次选 Naive UI，再不过退回"保留 antd + 只做令牌层"。这条判断在阶段 1 结束前必须落定。**

---

## 4. 分阶段计划（每阶段都以"门禁全绿"收口）

| 阶段 | 做什么 | 门禁与产出 | 依赖选型？ |
| --- | --- | --- | --- |
| **0 令牌层**（可立即做） | 建 `src/theme/tokens.ts`（TS 单一真源：色板/字号阶/行高/**4px 间距阶**/圆角/阴影/动效时长）→ 编译成 `--aeg-*` CSS 变量挂到 `:root` 与 `html[data-theme]`；把 478 处字面色按文件收敛到变量 | 新增源码门禁：**`src` 里除 `theme/tokens.ts` 外不许出现颜色字面值**（先立规则、再按文件清欠，每清一个文件收一个） | **否** |
| **1 双主题骨架** | `useTheme()`：`light`/`dark`/`system` 三态 + `localStorage` 持久化 + `matchMedia` 跟随系统；顶栏放切换器；布局骨架换成"可折叠侧栏（含智能体状态迷你区）+ 顶栏 + 栅格内容区" | 真机门禁加一档：**同一页在两个主题下各跑一遍现有 100 项判据**（几何类判据与主题无关，正好当换肤破绽的探针）；**阶段末必须完成 §3 的库验证** | 部分（骨架不依赖，组件替换依赖） |
| **2 组件层** | 按选型结论替换 16 个文件里的 202 处标签；同步改 e2e 里 51 处 `ant-*` 选择器 | 全部门禁绿；`uiTheme.spec.ts` 11 条**重写为新规范断言**（见 §5），**不删不 skip** | **是** |
| **3 逐页重做** | 顺序：`/dashboard`（风格标杆）→ `/monitor` → `/warnings` → `/map` → `/workflow` → `/metrics` → `/assistant`。每页必须交齐**加载/空/错三态** | 每页一次提交、一次全量门禁 + 整页截图目视复核（两主题各一张） | 是 |
| **4 多智能体预留** | 五段链路可视化（`decided_by` agent/local 如实标注"平台降级接管"）、人在回路可复用组件、`AgentMessage v1` TS 类型与 store 模块、编排页三类节点视觉区分 + 图例 | 类型层用契约 `contracts/AGENT_INTEGRATION_SPEC.v1.md` 对账；**不写任何智能体行为** | 否（但排在骨架后） |

---

## 5. 旧体系留下的资产与要重写的测试

**可继承（别重做）**：`e2e/ui-overlap.spec.ts` 100 项里绝大多数是**与风格无关的几何/可读性不变量**——
压叠、出屏、文字被裁、短词折行、对比度 AA、焦点可见、焦点不外泄、减动效、循环动画里的字淡出、空态必须带出口、
列表自查与新鲜度条。换风格时它们是抓"新写的东西照样重叠"的手，**保留并扩到双主题**。

**必须重写（不是删除）**：`src/uiTheme.spec.ts` 现有 11 条里有 4 条是**旧观感的宪法**，新体系下要么改口径要么换断言：
- `每个页面都挂 PageHero 深色横幅` → 新骨架没有"每页深色横幅"，改为"每页有页头区 + 面包屑"；
- `调色板只走蓝系（禁紫青粉）` → 新口径是"颜色一律来自令牌"，装饰色限制改为令牌级检查；
- `芯片 999 圆角统一` → 圆角进令牌，断言改为"圆角值必须取自令牌阶"；
- `主色令牌钉在 ConfigProvider #2563EB` → 改为"令牌真源在 `tokens.ts`，库的主题对象由它派生"。
其余 7 条（字体自托管、字重声明齐、样式入口挂载、导航覆盖全路由、深底文字过 AA、infinite 动画有 reduce 兜底、
过渡时长只走令牌）**原样保留**——它们与新风格无关，是硬约束。

**台账里挂着的两条待决，重做时一并处理**：
- 任务 #35（`NODE_STATE_STYLES` 是 antd 默认色板含紫/品红/青）→ 新令牌层落地后自然收敛，不必单独拍板；
- 任务 #41（「可选子系统」那格行内截断并把整行撑高）→ 归入阶段 3 的 `/dashboard` 重做，不单开批次。

---

## 6. 进度台账（勾选式）

- [x] 2026-10-07 基线实测：typecheck / 769 单测 / build 47.87s / e2e 100 项（HEAD `7cb8bd6`）
- [x] 2026-10-07 耦合面量化：202 处 `a-*`、16 个 import 文件、51 处 antd 类名断言、478 处硬编码颜色
- [x] 2026-10-07 组件库选型提案 + 推荐（Arco Vue 首选，Naive 次选）——**本文档**
- [ ] **等用户确认选型**（未确认前只做阶段 0）
- [x] 阶段 0：`src/theme/` 四层落地——`tokens.ts`（真源）→ `vars.ts`（`--aeg-*` 变量 + `applyTheme`）→ `antd.ts`（库主题派生）→ `useTheme.ts`（三态）
- [x] 阶段 0：源码门禁加**颜色棘轮**（`uiTheme.spec.ts` 第 12 条，基线 **458** = 探针实量，只减不增）
- [x] 阶段 0：令牌自检 `src/theme/tokens.spec.ts`（9 条：4px 网格、字号阶、两主题槽位对齐、**每一对文字/底色实算 AA**、风险五档可辨、派生层逐值等旧字面）
- [x] 阶段 0：三态用例 `src/theme/useTheme.spec.ts`（7 条：跟随系统/显式覆盖/翻系/持久化/隐私模式）
- [ ] 阶段 0 收尾（欠）：按文件把 458 处字面色收敛成 `var(--aeg-*)`（先 `theme.css` 103 处与 `App.vue` 47 处）
- [ ] **等用户确认选型**：阶段 0 的候选库探针实测（Arco 暗色是否纯 CSS 变量 / 表格 1366×768 / 体积变化）——要装新依赖，不自主动手
- [x] 阶段 1（前半）：`useTheme()` 三态 + `localStorage` 持久化 + `matchMedia` 跟随系统 + `main.ts` 首帧前落色
- [ ] 阶段 1：顶栏主题切换器（**本轮故意没上**：外壳还没接令牌，切了会只换 antd 组件、页面仍是浅底——见 §8 决策 3）
- [ ] 阶段 1：布局骨架（可折叠侧栏含智能体状态迷你区 + 顶栏 + 栅格）——**要先批准**
- [ ] 阶段 1：真机门禁扩到"两主题各跑一遍"
- [ ] 阶段 2：组件层替换 + e2e 里 51 处 `ant-*` 选择器迁移
- [ ] 阶段 2：`uiTheme.spec.ts` 11 条按新规范重写（4 条改口径、7 条保留）
- [ ] 阶段 3：七页逐页重做（每页交齐加载/空/错三态 + 两主题整页截图）
- [ ] 阶段 4：多智能体预留（状态区 / 链路可视化 / 人在回路组件 / AgentMessage v1 类型）

---

## 7. 需要用户确认的问题（三个，按优先级）

1. **组件库**：按推荐走 Arco Design Vue？还是先做阶段 0/1（令牌 + 双主题骨架，不依赖选型），等探针实测完再定？
   —— 影响面：202 处标签、16 个文件、51 处 e2e 断言、新增一个运行时依赖（MIT，解压 30.3 MB）。
2. **旧视觉的处置节奏**：`theme.css`（521 行）与 `PageHero` 深色横幅是**新骨架就位后再删**，还是现在就停用？
   —— 我倾向前者（先立后破，避免中途出现"没有门禁可跑"的空窗）；删除文件前会再征求同意。
3. **验收演示的时间点**：若有汇报日期，阶段 3 的页面顺序要按"演示主角优先"调（默认 `/dashboard` 先做，它是风格标杆）。

---

## 8. 阶段 0 交付记录（ADR，2026-10-07）

### 决策 1：令牌层先做，且不生成 CSS 文件

`src/theme/` 分四层，每层只干一件事：
`tokens.ts`（唯一真源，色值/间距/字号/圆角/阴影/动效都在这）→ `vars.ts`（派生成 `--aeg-*` 变量 + `applyTheme` 写 `data-theme`/`color-scheme`）→
`antd.ts`（把同一批令牌交给 ConfigProvider）→ `useTheme.ts`（三态与持久化）。
`App.vue` 里原来那 30 行字面 `ThemeConfig` 删掉了，换成 `antdTheme(resolved.value)`；`main.ts` 在 `mount()` 之前调 `initTheme()`，
避免首帧闪白。

**为什么不编译出一份 `tokens.css`**：生成的 CSS 文件是**第二个真源**——它一旦被手改或忘记重生成，"令牌改了画面没变"就又是两张皮。
运行时由 `vars.ts` 直接写 `documentElement.style`，源头只有一个函数。
派生层自己**不许带字面色**（门禁第 12 条逐文件检查），换库时只改 `antd.ts` 的函数体。

### 决策 2：颜色收敛用"棘轮"，不用一次清零

门禁第 12 条走 `src` 目录（排除 `theme/**` 与 `*.spec.ts`）、剥掉 `/* */` 与 `<!-- -->` 注释后数色值，**只许 ≤ 基线 458**。
458 由 `E:\tmp\color-ratchet-probe.mjs` 量得（与门禁同一套走目录 + 同一支正则，不是 `grep` 的 478 那个口径——那个数含注释与被删掉的 App.vue 字面块）。
分布：`styles/theme.css` 103、`App.vue` 47、`WorkflowView.vue` 36、`PageHero.vue` 31……31 个文件。
为什么不一次清零：逐页重做的批次每改一处就往下掉一个数，往上加才红；这样既不打断页面改造，也不许新代码继续就地写 hex。
`api/types.ts` 那 5 支风险基色**照样计入**（它是语义真源但不开后门，阶段 3 收进令牌层）。
两条变异检查做过：页面里加一处 hex → 红并点名最重的五个文件；只在派生层第三个文件加 hex → 红并点名该文件
（这一支专门验"带 `g` 的正则把 `lastIndex` 带给下一次检查"那个坑，所以判"有没有"用的是另起的不带 `g` 的表达式）。

### 决策 3：主题切换器**故意这一轮没上**

三态逻辑、持久化、跟随系统、首帧落色都已就位并有 7 条用例；但外壳（`App.vue` 的顶栏渐变、`theme.css` 的卡片）还没接令牌。
此时把切换器放上去，深色态只有 antd 组件变深、页面与横幅仍是浅底——正是需求里点名要避免的"白底补丁"。
所以顺序是：**先把外壳与页面的颜色收敛到 `var(--aeg-*)`（决策 2 的清欠），再上切换器**，那之后真机门禁跑双主题才有意义。

这条不是推测，在 4175 的构建产物里量过（`localStorage['aegis.theme']='dark'` 后刷新）：
`data-theme="dark"`、`--aeg-color-page` 已到 `#0B1220`、antd 的卡片文字翻成 `rgb(230,237,247)`，
可 `.app-shell` 的背景仍是 `rgb(242,244,248)`、卡片底还是 `theme.css` 那条 `rgba(255,255,255,.72)`——
**亮字压白底**，一整屏都读不了。这就是决策 3 要防的那个状态。

### 两个由新用例逮到的真 bug（不是重构副产品）

1. **灰字差 0.05 没过 AA**：`#6B7488` 落在白卡 4.69:1 勉强过，落到 `alter`(#F7F9FC) 只有 4.45、落到页面底 #F2F4F8 只有 4.26。
   压深到 `#626B7D` 后 card 5.36 / alter 5.08 / page 4.87，三处都过（`tokens.spec.ts` 把每一对都实算了一遍）。
2. **"跟随系统"根本不会跟着动**：`resolved` 里直接调 `matchMedia(...).matches`，那不是响应式依赖，`computed` 把第一次的结果**永久缓存**了；
   另外 `STORAGE_KEY` 的字面值里混进了一个 U+2026（写成 `'aeg…heme'`），持久化键从来不是 `aegis.theme`——
   表现为"选了深色、刷新就没了"。两处都是先把用例写红、再改绿；变异检查复跑过（把任一处退回去，用例立刻红）。

### 下一步（按依赖顺序）

1. 把 `theme.css`(103) 与 `App.vue`(47) 的字面色收敛成变量，让外壳真正能换肤 → 再上顶栏切换器；
2. 真机门禁扩到两主题各跑一遍（现有 100 项几何判据与风格无关，正好当换肤破绽探针）；
3. §7 三问等批准：组件库选型 / 旧视觉处置节奏 / 演示时间点（布局骨架属"大改先提案"，未批不动）。

**清欠时的变量名对照**（`applyTheme` 在 `:root` 上真写出来的就是这些，别再新起名）：
`--aeg-color-<槽位>`（槽位名即 `tokens.ts` 的 key：`page`/`card`/`elevated`/`sunken`/`alter`/`border`/`border-subtle`/`border-strong`/
`text`/`text-secondary`/`text-muted`/`text-inverse`/`brand`/`brand-hover`/`brand-active`/`brand-soft`/`brand-line`/`status-*`/`overlay-scrim`）、
`--aeg-risk-text-<1..5>`、`--aeg-shadow-sm|md|lg`、`--aeg-space-s1..s12`、`--aeg-font-<字号阶>`、`--aeg-weight-*`、`--aeg-lh-*`、
`--aeg-radius-sm|md|lg|pill`、`--aeg-motion-fast|base|slow|draw`、`--aeg-z-*`、`--aeg-header-height|sidebar-width|sidebar-collapsed|content-max`、
`--aeg-font-sans|mono`。

**已知的一处双轨（清欠时要一并收）**：`theme.css` 自己有一套 `--motion-fast/base/draw`（19 处 transition 在用），
令牌层现在又发一份 `--aeg-motion-*`。同名不同源就是下一次"每页按自己节奏动"的起点——
收敛做法：把 `theme.css` 里的 `--motion-*` 定义改成从 `--aeg-motion-*` 取值（保留旧名做别名、逐文件替换完再摘掉），
这一步归进上面第 1 项一起做，不单独开批次。

---

## 9. 本轮验证记录（2026-10-07 阶段 0 交付）

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| 类型 | `npm run typecheck` | exit 0（`vue-tsc --noEmit` 无输出） |
| 单测 | `npm run test` | **786 passed / 44 个文件**（exit 0） |
| 构建 | `npm run build` | **✓ built in 47.01s**（exit 0） |
| 令牌层 | `npx vitest run src/theme/` | 16 passed（tokens 9 + useTheme 7） |
| 源码门禁 | `npx vitest run src/uiTheme.spec.ts` | 12 passed（含新的颜色棘轮） |
| 后端 | `python -m ruff check src scripts tests` / `python -m pytest -q` | exit 0 / exit 0（本批零后端文件改动，只作回归确认） |
| 画面 | `AEGIS_API_TARGET=http://127.0.0.1:8321 npx playwright test ui-overlap` | 第一趟 **94 passed / 6 failed（35.5m）**，第二趟（加了自种夹具）**100 passed（21.1m）** |

### 那 6 项红是怎么查出来的（值得记，因为红的不是画面）

`warnings-detail`（四个视口）+ 弹层焦点那一套 + 运行时间线，全部卡在同一个地方：
`locator('role=button[name="详情"]')` 与 `.wf-run__pick` 等不到元素，180 秒超时。
对着后端读了一遍接口才发现：`GET /api/v1/warnings` 与 `/workflow/instances` 的 `count` 都是 **0**——
后端是本轮重起的，内存 store 从零开始，而这两项判据**一直在吃"这台机器的后端恰好有数据"这个未声明前提**。
上一轮 100 全绿是因为那个进程被灌过数。

处理方式不是"先把数灌进去再跑一遍交差"，而是**把前提写进门禁**：
新增 `ensureGateRows()`（`e2e/ui-overlap.spec.ts`），文件级 `test.beforeAll` 里查计数，
预警为 0 就 `POST /api/v1/drill/run {scenario:"surge"}`（最多三轮），实例为 0 就用门禁自种的
`GATE-UI大定义` `POST /api/v1/workflow/instances` 起一条；两种情况都在断言里写清"是几号状态码/为什么红"。
走的是真接口，不在组件里塞假数据；需要空态的那些项本来就用 `page.route` 自己灌空列表，加真数据不串台。
复跑证据：空 store 起步（`warnings count=0`、`instances count=0`）**100 项全绿**。

### 断言的变异检查（每条都先红过再绿）

- 颜色棘轮：页面里加一处 hex → 红，报"基线 458 涨到 459"并列出最重的五个文件；只在派生层第三个文件加 hex → 红并点名该文件（同时验掉"带 `g` 的正则把 `lastIndex` 带给下一次检查"这个坑）。
- 三态用例：把 `resolved` 改回"computed 里直读 `matchMedia`" → 翻系那条即红；把 `STORAGE_KEY` 改个后缀 → 持久化三条全红。
- 两次变异都由脚本改盘并 `finally` 回滚，回滚后 `git status` 只剩本轮真正的改动。
