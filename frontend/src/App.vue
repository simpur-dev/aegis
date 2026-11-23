<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import type { ThemeConfig } from 'ant-design-vue/es/config-provider/context'
import zhCN from 'ant-design-vue/es/locale/zh_CN'

import api from '@/api/client'
import ServiceTokenBar from '@/components/ServiceTokenBar.vue'
import AssistantDock from '@/components/assistant/AssistantDock.vue'
import { useAlerts } from '@/composables/useAlerts'
import { useEventStream } from '@/composables/useEventStream'
import { useHandlingSpace } from '@/composables/useHandlingSpace'
import { antdTheme } from '@/theme/antd'
import { NAV_GROUPS } from '@/router'
import { useTheme } from '@/theme/useTheme'
import { installSelectA11y } from '@/utils/selectA11y'

const route = useRoute()
const contentRegion = ref<HTMLElement | null>(null)
const immersive = computed(() => route.meta?.immersive === true)
const currentKey = computed(() => (route.name as string) ?? 'dashboard')

const health = ref<{ status: string; version: string } | null>(null)
const { events, connected, stalled } = useEventStream()
// 告警卡片挂在全应用唯一的这条共享流上；顶部条与助手抽屉（AssistantDock 内）共用同一份卡片状态
const { attachStream, urgentCards, badgeCount, refresh } = useAlerts()
attachStream({ events, connected })
void refresh()
// 同一条共享流同时喂告警卡与处置空间：按 subject 分流，不开第二条 SSE 连接。
const { attachStream: attachSpaceStream, refresh: refreshSpaces } = useHandlingSpace()
attachSpaceStream({ events, connected })
void refreshSpaces()

let timer: number | undefined
let uninstallSelectA11y: (() => void) | undefined

async function probe(): Promise<void> {
  try {
    health.value = await api.health()
  } catch {
    health.value = null
  }
}

onMounted(() => {
  void probe()
  uninstallSelectA11y = installSelectA11y()
  timer = window.setInterval(() => {
    if (document.visibilityState !== 'visible') return
    void probe()
  }, 15_000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
  uninstallSelectA11y?.()
})

/**
 * 组件库主题从设计令牌派生（真源 `src/theme/tokens.ts`，派生层 `src/theme/antd.ts`）。
 * 这里**不再写任何 hex 与时长**：以前这份 30 行字面值就是"页面各按各的颜色"的源头之一。
 * 浅色那一档的取值与改造前逐项相同（含 hover/active 往深走那两条，那是悬停态体检量出来的），
 * 所以"接令牌层"这一步不掺任何视觉变化——视觉回归由后面的逐页重做负责。
 */
const { resolved, setMode } = useTheme()
const theme = computed<ThemeConfig>(() => antdTheme(resolved.value))

/**
 * 主题只留「浅 / 深」两档（`system` 的解析能力仍在 useTheme 里，老用户的
 * `aegis.theme = 'system'` 照样按系统偏好落成亮/暗，只是不再给第三个按钮）。
 * 高亮按 `resolved` 判断：偏好是 system 时也要指向实际生效的那一档。
 */
const themeOptions = [
  { value: 'light' as const, short: '浅', label: '浅色（日常）' },
  { value: 'dark' as const, short: '深', label: '深色（大屏演示）' },
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
     两句话并排把顶栏右列撑到 374px，挤进中段导航 78px（真机 @1440 量的；当时那排还是步进器形态）。
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
    <div class="app-shell" :class="{ 'is-immersive': immersive }">
      <!-- 全局雾底：所有页面共用（含一张图与流程编排）——浅色/深色各一档，地图画布自己仍是深空底 -->
      <div class="app-backdrop" aria-hidden="true" />
      <div class="main-col">
        <a v-if="!immersive" href="#aegis-main" class="skip-content" @click.prevent="contentRegion?.focus()">跳到主要内容</a>
        <header v-if="!immersive" class="topbar">
          <div class="topbrand" data-testid="brand">
            <span class="brand-logo" aria-hidden="true"></span>
            <span class="topbrand__text">
              <span class="brand-name">AEGIS 山地灾害协同调控平台</span>
              <span class="brand-sub">西藏山地灾害多智能体协同调控</span>
            </span>
          </div>
          <nav class="topnav" aria-label="主导航">
            <template v-for="group in NAV_GROUPS" :key="group.key">
              <router-link
                v-for="item in group.items"
                :key="item.key"
                class="topnav__item"
                :class="{ 'is-on': currentKey === item.key }"
                :to="item.to"
                :title="item.label"
                :aria-current="currentKey === item.key ? 'page' : undefined"
                :data-testid="`nav-${item.key}`"
              >
                <span class="topnav__full">{{ item.label }}</span>
                <span class="topnav__short">{{ item.short }}</span>
              </router-link>
            </template>
            <!-- 智能助手是常驻工具，不占一级导航分组；仍是 App.vue 里唯一的字面跳转目标 -->
            <router-link
              class="topnav__item"
              :class="{ 'is-on': currentKey === 'assistant' }"
              to="/assistant"
              title="智能助手"
              aria-label="智能助手"
              data-testid="assistant-entry"
            >
              <span class="topnav__full">智能助手</span>
              <span class="topnav__short">助手</span>
            </router-link>
          </nav>

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
                <span class="status-pill__text">事件</span>
              </span>
            </div>
            <div class="theme-switch" role="group" aria-label="主题">
              <button
                v-for="opt in themeOptions"
                :key="opt.value"
                type="button"
                class="theme-switch__btn"
                :class="{ 'is-on': resolved === opt.value }"
                :aria-pressed="resolved === opt.value"
                :title="`${opt.label}主题`"
                :data-testid="`theme-${opt.value}`"
                @click="setMode(opt.value)"
              >
                {{ opt.short }}
              </button>
            </div>
          </div>
        </header>

        <!-- Ⅰ/Ⅱ 级告警的顶部常驻条：抽屉会被自动展开，这条是"关了抽屉也看得见"的兜底 -->
        <div
          v-if="urgentCards.length > 0 && route.name !== 'dashboard'"
          class="alert-banner"
          :class="{ 'alert-banner--assistant': route.name === 'assistant', 'alert-banner--editor': immersive }"
          data-testid="alert-banner"
        >
          <span class="alert-banner__dot" aria-hidden="true"></span>
          <span class="alert-banner__text" data-testid="alert-banner-text">
            {{ urgentCards.length }} 条红色/橙色告警未确认
          </span>
          <span class="alert-banner__meta">
            在案 {{ badgeCount }} 条 ｜ {{ route.name === 'assistant' ? '可在告警面板中处置' : '处置入口在右侧助手抽屉' }}
          </span>
        </div>

        <main
          id="aegis-main"
          ref="contentRegion"
          tabindex="-1"
          class="main-content"
          :class="{ 'is-immersive': immersive, 'is-map': route.name === 'map', 'is-desk': route.name === 'assistant' }"
        >
          <router-view />
          <!-- 服务令牌条常驻页脚：一台机器填一次，但"这台开了鉴权"必须每页都说得出口（§4.8.1） -->
          <ServiceTokenBar v-if="!immersive" />
          <!-- 页脚跟着内容走，不钉在视口底：钉着的话每一页都永久吃掉一条，
               而编排页那种"占满一屏"的布局还要去猜它的高度 -->
          <footer v-if="!immersive" class="footer">
            AEGIS · 山地灾害协同调控平台 ｜ 值班 · 监测 · 处置 · 复盘
          </footer>
        </main>
      </div>
      <!-- 全站悬浮的智能助手：所有页面都有（含沉浸式编排页），位置/开合自己持久化 -->
      <!-- 助手页本身就是全屏会话：再叠一份抽屉会出现两份共享同一会话的时间线 -->
      <AssistantDock v-if="route.name !== 'assistant'" :immersive="immersive" :avoid-map-bar="route.name === 'map'" />
    </div>
  </a-config-provider>
</template>

<style>
.app-shell {
  display: flex;
  /* 外壳定高：顶栏固定，正文区自己滚（侧栏已退役，不再有第二列） */
  height: 100vh;
  overflow: hidden;
  background: var(--aeg-color-page);
  color: var(--aeg-color-text);
}

.topnav {
  grid-area: nav;
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s1);
  min-width: 0;
  border-top: 1px solid var(--aeg-color-border);
  overflow-x: auto;
  scrollbar-width: none;
}
.topnav::-webkit-scrollbar {
  display: none;
}
.topnav__item {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  height: 36px;
  padding: 0 var(--aeg-space-s4);
  border-radius: var(--aeg-radius-md);
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-base);
  font-weight: var(--aeg-weight-semibold);
  text-decoration: none;
  white-space: nowrap;
  transition:
    background-color var(--aeg-motion-base) ease,
    color var(--aeg-motion-base) ease;
}
.topnav__item:hover {
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-text);
}
.topnav__item.is-on {
  background: var(--aeg-color-masthead-tab);
  box-shadow: 0 3px 12px var(--aeg-color-brand-soft);
  color: var(--aeg-color-masthead-tab-ink);
}

html[data-theme='dark'] .topnav__item.is-on {
  background: linear-gradient(120deg, var(--aeg-color-masthead-tab), var(--aeg-color-brand));
}

.topbrand {
  grid-area: brand;
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s3);
  min-width: 0;
}
.brand-logo {
  position: relative;
  flex: 0 0 40px;
  height: 40px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-brand-soft);
}
.brand-logo::before,
.brand-logo::after {
  content: '';
  position: absolute;
  inset: 8px 5px;
  background: var(--aeg-color-overview-accent);
  clip-path: polygon(0 93%, 32% 19%, 49% 49%, 64% 25%, 100% 93%);
}
.brand-logo::after {
  background: var(--aeg-color-text);
  clip-path: polygon(23% 39%, 32% 19%, 41% 36%, 33% 32%, 29% 41%, 26% 36%, 56% 39%, 64% 25%, 73% 42%, 65% 35%, 61% 43%, 59% 38%);
}
.topbrand__text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  line-height: var(--aeg-lh-tight);
}
.brand-name {
  font-family: var(--aeg-font-display);
  font-size: var(--aeg-font-md);
  font-weight: var(--aeg-weight-bold);
  letter-spacing: 0.02em;
  color: var(--aeg-color-text);
}
.brand-sub {
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-secondary);
  white-space: nowrap;
}

.main-col {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  height: 100vh;
}
.topbar {
  position: relative;
  z-index: 1;
  isolation: isolate;
  display: grid;
  flex: 0 0 var(--aeg-header-height);
  grid-template: 'brand facts' 1fr 'nav nav' 1fr / minmax(0, 1fr) auto;
  column-gap: var(--aeg-space-s3);
  padding-inline: var(--aeg-space-s6);
  border-bottom: 1px solid var(--aeg-color-border);
  background: var(--aeg-color-masthead-bg);
}
html[data-theme='dark'] .topbar::before {
  content: '';
  position: absolute;
  z-index: -1;
  inset: 0 0 0 auto;
  width: min(36vw, 420px);
  opacity: 0.25;
  background: url('/backgrounds/tibet-relief.webp') center / cover;
  mask-image: linear-gradient(90deg, transparent, black 30%);
}
.topbar-right {
  grid-area: facts;
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
  transition:
    background-color var(--aeg-motion-base) ease,
    color var(--aeg-motion-base) ease;
}
.theme-switch__btn:hover {
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-text);
}
.theme-switch__btn.is-on {
  /* 半透明白色胶囊：压在雾底上有层次，但不像实心白块那样抢戏 */
  background: var(--aeg-color-topbar-pill-bg);
  box-shadow: inset 0 0 0 1px var(--aeg-color-glass-border);
  color: var(--aeg-color-text);
}

.main-content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: var(--aeg-space-s5) var(--aeg-space-s6);
  transition: margin-right var(--aeg-motion-base) ease;
}
.skip-content {
  position: absolute;
  top: var(--aeg-space-s2);
  left: var(--aeg-space-s4);
  z-index: var(--aeg-z-header);
  padding: var(--aeg-space-s2) var(--aeg-space-s3);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-masthead-tab);
  color: var(--aeg-color-masthead-tab-ink);
  transform: translateY(-160%);
}
.skip-content:focus {
  transform: none;
}

/* Ⅰ/Ⅱ 级告警顶部条：红色系语义槽（status-danger），只做"有事"的宣告，不做操作面 */
.alert-banner {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s3);
  margin: 0 var(--aeg-space-s6);
  padding: var(--aeg-space-s2) var(--aeg-space-s4);
  border: 1px solid var(--aeg-color-status-danger);
  border-radius: var(--aeg-radius-md);
  background: var(--aeg-color-brand-soft);
}
.alert-banner__dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: var(--aeg-color-status-danger);
}
.alert-banner__text {
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  color: var(--aeg-color-status-danger);
}
.alert-banner__meta {
  margin-left: auto;
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
}
/* 助手抽屉打开时把主内容整体往左挤（抽屉本身固定贴右，不再盖在内容上）；
   宽度只读 --assistant-drawer-w 一个真源，抽屉左侧拖拽时同步跟随 */
html.assistant-drawer-open .main-content {
  margin-right: var(--assistant-drawer-w, clamp(320px, 25vw, 520px));
}
@media (max-width: 720px) {
  html.assistant-drawer-open .main-content {
    margin-right: 0;
  }
  .alert-banner--assistant {
    margin-inline: var(--aeg-space-s3);
  }
  .alert-banner--assistant .alert-banner__meta {
    display: none;
  }
}
/* 拖宽度手柄时关掉过渡：否则主内容会滞后于指针 */
html.assistant-resizing .main-content {
  transition: none;
}
/* 一张图全屏：主内容不再留内边距，地图自己铺满顶栏以下 */
.main-content.is-map {
  padding: 0;
}
/* 值班台主内容改竖排 flex：内容不足一屏时会话吃掉令牌条与页脚之外的全部高度，
   内容更高时整页滚动，令牌条与页脚跟在全部内容之后 */
.main-content.is-desk {
  display: flex;
  flex-direction: column;
  /* 值班台两侧多留一倍呼吸位（24→48），会话整体右移，页面不贴边 */
  padding-left: var(--aeg-space-s12);
  padding-right: var(--aeg-space-s12);
}
.main-content.is-desk > :first-child {
  /* auto 基线 + 不压缩：零基线(flex:1)会把外壳压到视口高，
     更高的会话溢出后与令牌条/页脚叠在一起 */
  flex: 1 0 auto;
}
.main-content.is-immersive {
  overflow: hidden;
  padding: 0;
}
.alert-banner--editor {
  padding-block: var(--aeg-space-s1);
}
@media (max-width: 720px) {
  .alert-banner--editor {
    margin-inline: var(--aeg-space-s3);
  }
  .alert-banner--editor .alert-banner__meta {
    display: none;
  }
}
.main-col:has(.main-content.is-immersive),
.app-shell.is-immersive {
  height: 100vh;
}
.app-shell.is-immersive {
  /* 编辑器外壳也让雾底透上来：画布与面板自己带底色，这里不再铺一整块深色板 */
  background: transparent;
}
.footer {
  padding: var(--aeg-space-s4);
  background: transparent;
  text-align: center;
  font-size: var(--aeg-font-sm);
  color: var(--aeg-color-text-secondary);
}

/* 助手入口现在是顶栏导航里的一项（.topnav__item），不再有单独的悬浮/侧栏按钮。 */

/* 窄档只收状态文字：少显示一点，不是互相压上去。
   侧栏是定宽的，顶栏右列在 ≤1290 把胶囊文字收成圆点即可，页名不受影响。
   收文字用读屏可见的裁剪而不是 display:none——状态是信息，视觉收了读屏不该跟着丢。 */
.topnav__short {
  display: none;
}
@media (max-width: 780px) {
  .status-pill__text {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    padding: 0;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
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
/* 手机档：导航用短名、品牌副标题让位，顶栏才装得下（真机 390px 量到互相挤压截断） */
@media (max-width: 640px) {
  .topnav__full {
    display: none;
  }
  .topnav__short {
    display: inline;
  }
  .topnav__item {
    padding-inline: var(--aeg-space-s2);
  }
  .brand-sub {
    display: none;
  }
  .brand-name {
    font-size: var(--aeg-font-sm);
  }
  .topbar,
  .main-content,
  .main-content.is-desk {
    padding-inline: var(--aeg-space-s3);
  }
  .topbar,
  .topbar-right,
  .topbrand {
    gap: var(--aeg-space-s2);
  }
  .brand-logo {
    flex-basis: 32px;
    height: 32px;
  }
  .brand-logo::before,
  .brand-logo::after {
    inset: 5px 4px;
  }
}

/* ——— 全局雾底（2026-11-19 用户裁决恢复） ———
   素材是离线资产 `public/backgrounds/aegis-horizon.webp`（不外链任何 CDN）：
   淡蓝→粉紫的地平线渐变，模糊 46px + 放大 1.1 后只剩柔和的色团，不抢卡片的字。
   渐变兜底用画布令牌，两主题各一套。所有页面共用（含一张图与流程编排）。
   模糊走背景图自身的 filter（一次性栅格化），不用 backdrop-filter——后者每帧重采样。 */
.app-shell {
  background: transparent;
}
.app-backdrop {
  position: fixed;
  inset: 0;
  z-index: 0;
  overflow: hidden;
  pointer-events: none;
  background: linear-gradient(135deg, var(--aeg-color-canvas-from) 0%, var(--aeg-color-canvas-via) 45%, var(--aeg-color-canvas-to) 100%);
}
.app-backdrop::before {
  content: '';
  position: absolute;
  inset: -48px;
  background-image: url('/backgrounds/aegis-horizon.webp');
  background-size: cover;
  background-position: center;
  filter: blur(46px) saturate(1.12);
  transform: scale(1.1);
  opacity: 0.95;
}
/* 光斑随浅深主题调整，保留面板之间的层次。 */
.app-backdrop::after {
  content: '';
  position: absolute;
  inset: 0;
  background:
    radial-gradient(circle at 18% 8%, var(--aeg-color-canvas-glow), transparent 55%),
    radial-gradient(circle at 88% 78%, var(--aeg-color-brand-soft), transparent 60%);
  opacity: 0.55;
}
html[data-theme='dark'] .app-backdrop::before {
  filter: blur(46px) brightness(0.4) saturate(1.35);
  opacity: 0.22;
}
html[data-theme='dark'] .app-backdrop::after {
  background:
    radial-gradient(ellipse at 54% 75%, var(--aeg-color-canvas-glow), transparent 55%),
    radial-gradient(ellipse at 92% 41%, var(--aeg-color-brand-soft), transparent 52%);
  opacity: 0.8;
}

.main-col {
  position: relative;
  z-index: 1;
}
</style>
