<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import type { ThemeConfig } from 'ant-design-vue/es/config-provider/context'
import zhCN from 'ant-design-vue/es/locale/zh_CN'

import api from '@/api/client'
import type { AgentInfo } from '@/api/types'
import ServiceTokenBar from '@/components/ServiceTokenBar.vue'
import { useEventStream } from '@/composables/useEventStream'
import { antdTheme } from '@/theme/antd'
import { NAV_GROUPS } from '@/router'
import { useTheme } from '@/theme/useTheme'

/**
 * 外壳（2026-10-17 信息架构收并）：侧栏导航取代顶栏一排平级页签。
 *
 * 为什么收：八页平铺时三类受众（值班业务 / 运维取证 / 演示玩具）混在同一排，
 * 顶栏那个"流程步进器"样式还暗示着并不存在的顺序——外人打开无从下手。
 * 现在侧栏按受众分两组（值班主线 / 系统），顶栏只留"我在哪页 + 后端与事件流状态 + 主题"，
 * 智能助手是每页可见的悬浮入口（合并进抽屉之前先指向 /assistant 页）。
 *
 * 颜色一律读 `--aeg-*` 令牌（真源 src/theme/tokens.ts），动效只走 `--aeg-motion-*`：
 * 这两条由 src/uiTheme.spec.ts 的棘轮门禁钉住，写字面色就是往回退。
 */

const route = useRoute()
const currentKey = computed(() => (route.name as string) ?? 'dashboard')
const pageTitle = computed(() => (route.meta?.title as string) ?? 'AEGIS')

const SIDEBAR_KEY = 'aegis.sidebar'
const collapsed = ref(false)
try {
  collapsed.value = localStorage.getItem(SIDEBAR_KEY) === 'closed'
  /* 没有存过偏好时，窄屏（值班平板）默认收起：232px 侧栏在 900px 宽只剩 668px 正文，
     而这一档本来就要看遥测表。存过偏好就不猜侧，用户选过就算数。 */
  if (localStorage.getItem(SIDEBAR_KEY) === null && window.innerWidth < 1000) collapsed.value = true
} catch {
  /* 隐私模式或存储被禁：用展开态，不拦页面 */
}
function toggleCollapse(): void {
  collapsed.value = !collapsed.value
  try {
    localStorage.setItem(SIDEBAR_KEY, collapsed.value ? 'closed' : 'open')
  } catch {
    /* 存不下就算了，切换本身照样生效 */
  }
}

const health = ref<{ status: string; version: string } | null>(null)
const { events, connected, stalled } = useEventStream()

/**
 * 侧栏底部的智能体迷你状态区：五智能体在线/降级/离线是这一页最该常驻的事实，
 * 但取不到就说取不到——`agentsLoaded` 为 false 时摆 "…"，不拿 0 冒充"都没上线"。
 */
const agents = ref<AgentInfo[]>([])
const agentsLoaded = ref(false)
const agentsOnline = computed(() => agents.value.filter((a) => a.healthy).length)
const agentsTone = computed(() => {
  if (!agentsLoaded.value) return 'idle' as const
  if (agents.value.length === 0) return 'bad' as const
  return agentsOnline.value === agents.value.length ? ('ok' as const) : ('warn' as const)
})
const agentsText = computed(() =>
  agentsLoaded.value ? `智能体 ${agentsOnline.value}/${agents.value.length} 在线` : '智能体状态未取到',
)
const agentsHint = computed(() =>
  agentsLoaded.value
    ? agents.value.map((a) => `${a.agent_id}：${a.healthy ? '在线' : '不健康'}`).join('｜')
    : '智能体清单还没取到：刷新或检查后端',
)

let timer: number | undefined

async function probe(): Promise<void> {
  try {
    health.value = await api.health()
  } catch {
    health.value = null
  }
  try {
    const list = await api.agents()
    agents.value = list.items
    agentsLoaded.value = true
  } catch {
    agentsLoaded.value = false
  }
}

onMounted(() => {
  void probe()
  timer = window.setInterval(() => {
    if (document.visibilityState !== 'visible') return
    void probe()
  }, 15_000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
})

/**
 * 组件库主题从设计令牌派生（真源 `src/theme/tokens.ts`，派生层 `src/theme/antd.ts`）。
 * 这里**不再写任何 hex 与时长**：以前这份 30 行字面值就是"页面各按各的颜色"的源头之一。
 * 浅色那一档的取值与改造前逐项相同（含 hover/active 往深走那两条，那是悬停态体检量出来的），
 * 所以"接令牌层"这一步不掺任何视觉变化——视觉回归由后面的逐页重做负责。
 */
const { resolved, mode: pref, setMode } = useTheme()
const theme = computed<ThemeConfig>(() => antdTheme(resolved.value))

/**
 * 切换器三档都在明面上，不做"点一下翻一档"的循环钮——汇报现场没人知道还要再点几次。
 * 标签只许一个字：右列轨道 @1440 只有 295px，胶囊文案最长那档已经占掉 285px（真机量的），
 * 完整说法走 `title` 与 `aria-label`。
 */
const themeOptions = [
  { value: 'light' as const, short: '浅', label: '浅色（日常）' },
  { value: 'dark' as const, short: '深', label: '深色（大屏演示）' },
  { value: 'system' as const, short: '随', label: '跟随系统' },
]

/**
 * 事件流徽标的三条真话。
 *
 * 真机把后端进程杀掉之后量到：`/healthz` 立刻不可达、页面取数一直在失败，
 * 可这个徽标仍然写着"事件流已连接"——浏览器不会因为上游进程死了而报错，那条流已经是僵尸。
 * 所以后端都不在线时不许说连着；心跳静默（前端自己判死并重连）时把"没心跳"说出来。
 */
const streamLabel = computed<string>(() => {
  /* 括号里那句"（后端不可达）"删掉了：左边那颗胶囊此时就写着"后端不可达"，
     两句话并排把顶栏右列撑到 374px，挤进中间步进器 78px（真机 @1440 量的）。
     原因还在，挪到 title 里，hover 看得到。 */
  if (health.value === null) return '事件流已中断'
  if (connected.value) return '事件流已连接'
  return stalled.value ? '事件流没心跳，正在重连' : '事件流重连中'
})
/** 胶囊窄起来会省略号，完整那句放 title 里。 */
const streamHint = computed<string>(() => (health.value === null ? '事件流已中断：后端不可达' : streamLabel.value))
/** 单胶囊里三句话各自的全称（版本号从可见文案挪到这里）。 */
const statusTitle = computed<string>(() =>
  [
    health.value ? `后端在线 v${health.value.version}` : '后端不可达：取数与操作都会失败',
    streamHint.value,
    `最近事件 ${events.value.length} 条`,
  ].join(' ｜ '),
)
</script>

<template>
  <!-- locale 走中文：体检判据在故障态量出监测台账、地图清单、指标表三处
       antd 默认英文空态 "No data"（此前是一张张截图发现再逐页补 emptyText）。
       逐页补只能盖住写过的表，语言包一次盖住整类。 -->
  <a-config-provider :theme="theme" :locale="zhCN">
    <div class="app-shell">
      <!-- 侧栏：品牌 + 分组导航 + 智能体迷你状态 + 收起钮。宽度走布局令牌
           （--aeg-sidebar-width 232 / --aeg-sidebar-collapsed 64），不许在这儿写死像素。 -->
      <aside class="sidebar" :class="{ 'is-collapsed': collapsed }" data-testid="sidebar">
        <div class="side-brand">
          <span class="brand-logo" aria-hidden="true">A</span>
          <span class="brand-name">AEGIS</span>
          <span class="brand-sub">山地灾害协同调控</span>
        </div>

        <nav class="side-scroll" aria-label="主导航">
          <div v-for="group in NAV_GROUPS" :key="group.key" class="side-group">
            <p class="side-group__label">{{ group.label }}</p>
            <router-link
              v-for="item in group.items"
              :key="item.key"
              class="side-item"
              :class="{ active: currentKey === item.key }"
              :to="item.to"
              :title="item.label"
              :aria-label="item.label"
              :aria-current="currentKey === item.key ? 'page' : undefined"
              :data-testid="`side-${item.key}`"
            >
              <span class="side-item__label">{{ collapsed ? item.short : item.label }}</span>
            </router-link>
          </div>
        </nav>

        <div class="side-agents" :class="`is-${agentsTone}`" :title="agentsHint" data-testid="side-agents">
          <span class="status-dot" :class="agentsTone === 'idle' ? '' : agentsTone"></span>
          <span class="side-agents__text">{{ agentsText }}</span>
        </div>

        <button
          type="button"
          class="side-collapse"
          :aria-expanded="!collapsed"
          data-testid="side-collapse"
          @click="toggleCollapse"
        >
          {{ collapsed ? '展开侧栏' : '收起侧栏' }}
        </button>
      </aside>

      <div class="main-col">
        <!-- 顶栏只留三件事：我在哪页（左）、后端与事件流状态（中右）、主题（右）。
             页名放这里是给"从侧栏点进来之后忘了去了哪"的人一个固定锚点。 -->
        <header class="topbar">
          <div class="topbar-left">
            <h1 class="topbar-title">{{ pageTitle }}</h1>
          </div>
          <div class="topbar-right">
            <div class="nav-status" :title="statusTitle" data-testid="nav-status">
              <span class="status-fact">
                <span class="status-dot" :class="health ? 'ok' : 'bad'"></span>
                <span class="status-pill__text">{{ health ? '后端在线' : '后端不可达' }}</span>
              </span>
              <span class="status-fact" data-testid="stream-badge">
                <span class="status-dot" :class="connected && health ? 'live' : 'bad'"></span>
                <span class="status-pill__text">{{ streamLabel }}</span>
              </span>
              <span class="status-fact" data-testid="events-badge" title="最近事件条数">
                <span class="events-count">{{ events.length }}</span>
                <span>事件</span>
              </span>
            </div>
            <div class="theme-switch" role="group" aria-label="主题">
              <button
                v-for="opt in themeOptions"
                :key="opt.value"
                type="button"
                class="theme-switch__btn"
                :class="{ 'is-on': pref === opt.value }"
                :aria-pressed="pref === opt.value"
                :title="`${opt.label}主题`"
                :data-testid="`theme-${opt.value}`"
                @click="setMode(opt.value)"
              >
                {{ opt.short }}
              </button>
            </div>
          </div>
        </header>

        <main class="main-content">
          <router-view />
          <!-- 服务令牌条常驻页脚：一台机器填一次，但"这台开了鉴权"必须每页都说得出口（§4.8.1） -->
          <ServiceTokenBar />
          <!-- 页脚跟着内容走，不钉在视口底：钉着的话每一页都永久吃掉一条，
               而编排页那种"占满一屏"的布局还要去猜它的高度 -->
          <footer class="footer">
            AEGIS · Adaptive Emergency Geo-hazard Intelligence System ｜ 西藏山地灾害多智能体协同调控技术与平台
          </footer>
        </main>
      </div>

      <!-- 智能助手常驻入口：每一页都点得到。合并成右侧抽屉之前，先指向 /assistant 页。 -->
      <router-link class="assistant-fab" to="/assistant" title="智能助手" data-testid="assistant-fab">
        助手
      </router-link>
    </div>
  </a-config-provider>
</template>

<style>
.app-shell {
  display: flex;
  /* 外壳定高、侧栏与正文自己安排滚动：正文区自己滚，侧栏整高不滚（内容多了内部滚） */
  height: 100vh;
  overflow: hidden;
  background: var(--aeg-color-page);
  color: var(--aeg-color-text);
}

/* —— 侧栏 ——
   分组导航按受众分（值班主线 / 系统），不再是一排平级页签：
   旧顶栏把值班业务、运维取证、演示玩具混在同一列，还摆成"流程顺序"的误导形状。 */
.sidebar {
  display: flex;
  flex-direction: column;
  flex: 0 0 auto;
  width: var(--aeg-sidebar-width);
  border-right: 1px solid var(--aeg-color-border);
  background: var(--aeg-color-card);
  transition: width var(--aeg-motion-base) ease;
}
.sidebar.is-collapsed {
  width: var(--aeg-sidebar-collapsed);
}
.side-brand {
  display: flex;
  flex-direction: column;
  gap: var(--aeg-space-s1);
  padding: var(--aeg-space-s4) var(--aeg-space-s4) var(--aeg-space-s3);
  border-bottom: 1px solid var(--aeg-color-border);
}
.brand-link {
  display: inline-flex;
  align-items: center;
  gap: var(--aeg-space-s3);
  text-decoration: none;
}
.brand-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-brand);
  color: var(--aeg-color-text-inverse);
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-bold);
}
.brand-name {
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-bold);
  letter-spacing: 0.02em;
  color: var(--aeg-color-text);
}
.brand-sub {
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 收起态只留徽标：副标题先让位（它不表达状态，页名在顶栏也有一份） */
.sidebar.is-collapsed .brand-name,
.sidebar.is-collapsed .brand-sub {
  display: none;
}

.side-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--aeg-space-s3) var(--aeg-space-s2);
}
.side-group + .side-group {
  margin-top: var(--aeg-space-s4);
}
.side-group__label {
  margin: 0 0 var(--aeg-space-s1);
  padding: 0 var(--aeg-space-s2);
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text-muted);
}
.side-item {
  display: flex;
  align-items: center;
  min-height: 36px;
  margin-bottom: 2px;
  padding: 0 var(--aeg-space-s2);
  border-radius: var(--aeg-radius-md);
  color: var(--aeg-color-text-secondary);
  text-decoration: none;
  white-space: nowrap;
  transition: background-color var(--aeg-motion-base) ease, color var(--aeg-motion-base) ease;
}
.side-item__label {
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  overflow: hidden;
  text-overflow: ellipsis;
}
.side-item:not(.active):hover {
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-text);
}
.side-item.active {
  background: var(--aeg-color-brand);
  color: var(--aeg-color-text-inverse);
}
.sidebar.is-collapsed .side-item {
  justify-content: center;
  padding: 0;
}

/* 智能体迷你状态区：侧栏常驻的"平台还活着吗"答案。
   idle 档（没取到）用灰点 + "未取到"，不许画成绿的——那会是假装在线。 */
.side-agents {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s2);
  margin: 0 var(--aeg-space-s2) var(--aeg-space-s2);
  padding: var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-sunken);
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-text-secondary);
}
.side-agents__text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.side-agents.is-ok .status-dot {
  background: var(--aeg-color-status-success);
}
.side-agents.is-warn .status-dot {
  background: var(--aeg-color-status-warning);
}
.side-agents.is-bad .status-dot {
  background: var(--aeg-color-status-danger);
}
.sidebar.is-collapsed .side-agents__text {
  display: none;
}

.side-collapse {
  flex: 0 0 auto;
  margin: 0 var(--aeg-space-s2) var(--aeg-space-s3);
  padding: var(--aeg-space-s2);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font-family: inherit;
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-semibold);
  cursor: pointer;
  transition: background-color var(--aeg-motion-base) ease, color var(--aeg-motion-base) ease;
}
.side-collapse:hover {
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-text);
}

/* —— 顶栏 ——
   只留"我在哪页 / 后端与事件流状态 / 主题"三件事，不透明 card 底 + 一条 border 分隔：
   顶栏下面是会滚动的表格与 Cesium 画面，模糊那一层既贵（每帧重算）又让描边发灰。 */
.main-col {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--aeg-space-s4);
  height: var(--aeg-header-height);
  padding: 0 var(--aeg-space-s6);
  background: var(--aeg-color-card);
  border-bottom: 1px solid var(--aeg-color-border);
  box-shadow: var(--aeg-shadow-sm);
}
.topbar-left {
  min-width: 0;
}
.topbar-title {
  margin: 0;
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-bold);
  color: var(--aeg-color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.topbar-right {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--aeg-space-s3);
  min-width: 0;
  max-width: 100%;
}

/* 状态胶囊：一枚里装三条真话（min-width 180 / padding 8px 12px / 999 圆角）。
   此前是三枚独立胶囊，@1440 就装不下：在线文案 316px、故障文案 374px，都大于 295px 的列宽，
   只能靠省略号截断（真机截图里 "后端在线 v0…" / "事件流已…"）。合成一枚后 273px / 285px，两档都放得下。 */
.nav-status {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  min-width: 180px;
  max-width: 100%;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  overflow: hidden;
}
.status-fact {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  flex: 0 1 auto;
  min-width: 0;
  white-space: nowrap;
}
/* 三条之间各有一道发丝分隔线 */
.status-fact + .status-fact {
  flex: 0 1 auto;
  padding-left: 10px;
  border-left: 1px solid var(--aeg-color-border);
}
/* 挤不下时省略号收尾（全称在 title 里），不许无声裁字 */
.status-pill__text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 状态点：语义色走令牌，去掉上一版那圈 10px 光晕（"发光克制"，且光晕在深色底上会糊成一片） */
.status-dot {
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--aeg-color-border-strong);
}
.status-dot.ok {
  background: var(--aeg-color-status-success);
}
.status-dot.live {
  background: var(--aeg-color-brand);
  animation: status-pulse 1.5s infinite;
}
.status-dot.bad {
  background: var(--aeg-color-status-danger);
}
@keyframes status-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.5;
  }
}
.events-count {
  font-family: var(--aeg-font-mono);
  font-weight: var(--aeg-weight-bold);
  color: var(--aeg-color-text);
}

/* 主题切换器：与侧栏选中项同一族"轨道 + 选中格"的分段写法。
   选中格 `elevated` 底 + 主色字——字色取 `brand-hover` 而不是 `brand`：深色主题下 brand(#3B82F6)
   压在 elevated(#16223A) 上只有 4.05:1，12px 的字要 4.5:1；提亮的 brand-hover(#60A5FA) 才够。 */
.theme-switch {
  display: flex;
  align-items: center;
  flex: 0 0 auto;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-sunken);
}
.theme-switch__btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 24px;
  padding: 0;
  border: none;
  border-radius: var(--aeg-radius-pill);
  background: transparent;
  color: var(--aeg-color-text-secondary);
  font-family: inherit;
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  cursor: pointer;
  transition: background-color var(--aeg-motion-base) ease, color var(--aeg-motion-base) ease;
}
.theme-switch__btn:hover {
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-text);
}
.theme-switch__btn.is-on {
  background: var(--aeg-color-elevated);
  box-shadow: var(--aeg-shadow-sm);
  color: var(--aeg-color-brand-hover);
}

.main-content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--aeg-space-s5) var(--aeg-space-s6);
}
.footer {
  padding: var(--aeg-space-s4);
  background: transparent;
  text-align: center;
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-secondary);
}

/* 助手悬浮入口：每页都点得到，压在内容之上但不挡操作（右下角是滚动条位之外的留白）。
   它替代了原来一级页签里的"智能助手"——助手是随叫随到的工具，不该占一级导航。 */
.assistant-fab {
  position: fixed;
  right: var(--aeg-space-s6);
  bottom: var(--aeg-space-s6);
  z-index: var(--aeg-z-header);
  display: inline-flex;
  align-items: center;
  min-height: 40px;
  padding: 0 var(--aeg-space-s5);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-brand);
  color: var(--aeg-color-text-inverse);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-bold);
  text-decoration: none;
  box-shadow: var(--aeg-shadow-md);
  transition: background-color var(--aeg-motion-base) ease, transform var(--aeg-motion-base) ease;
}
.assistant-fab:hover {
  background: var(--aeg-color-brand-hover);
  transform: translateY(-1px);
}

/* 窄档只收状态文字：少显示一点，不是互相压上去。
   侧栏是定宽的，顶栏右列在 ≤1290 把胶囊文字收成圆点即可，页名不受影响。 */
@media (max-width: 1290px) {
  .status-pill__text {
    display: none;
  }
  /* 文字收了，分隔线与它的留白也得跟着收——不然三个点之间空荡荡，看着像坏了 */
  .status-fact + .status-fact {
    padding-left: 0;
    border-left: none;
  }
  .nav-status {
    gap: 7px;
    min-width: 0;
    padding: 8px 10px;
  }
}
</style>
