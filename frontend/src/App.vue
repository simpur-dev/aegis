<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import type { ThemeConfig } from 'ant-design-vue/es/config-provider/context'
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import {
  ApartmentOutlined,
  BarChartOutlined,
  DashboardOutlined,
  RobotOutlined,
} from '@ant-design/icons-vue'

import api from '@/api/client'
import ServiceTokenBar from '@/components/ServiceTokenBar.vue'
import AssistantDock from '@/components/assistant/AssistantDock.vue'
import { useAlerts } from '@/composables/useAlerts'
import { useEventStream } from '@/composables/useEventStream'
import { useHandlingSpace } from '@/composables/useHandlingSpace'
import { antdTheme } from '@/theme/antd'
import { WORKSPACES, workspaceFor } from '@/router'
import { useTheme } from '@/theme/useTheme'
import { installSelectA11y } from '@/utils/selectA11y'

const route = useRoute()
const contentRegion = ref<HTMLElement | null>(null)
const navTrack = ref<HTMLElement | null>(null)
const navHighlight = ref<HTMLElement | null>(null)
const navShine = ref<HTMLElement | null>(null)
const immersive = computed(() => route.meta?.immersive === true)
const workspace = computed(() => workspaceFor(route))
const mobileItems = WORKSPACES.filter((item) => item.key !== 'assistant')
const mobileIcons = { duty: DashboardOutlined, research: ApartmentOutlined, data: BarChartOutlined }
const assistantNav = WORKSPACES[3]

function workspaceCurrent(key: string, path: string): 'page' | 'location' | undefined {
  if (workspace.value.key !== key) return undefined
  return route.path === path ? 'page' : 'location'
}

let glideMotion: Animation | undefined
let shineMotion: Animation | undefined
let navResize: ResizeObserver | undefined
let reduceMotion: MediaQueryList | undefined

function placeNavHighlight(animate = false): void {
  const track = navTrack.value
  const highlight = navHighlight.value
  const shine = navShine.value
  const selected = track?.querySelector<HTMLElement>('.topnav__item[aria-current]')
  if (!track || !highlight || !shine || !selected?.offsetWidth) return

  // 连点时从高亮的当前画面位置接续，避免跳回上一次的目标位置。
  const frame = animate && reduceMotion?.matches === false && highlight.offsetWidth
    ? highlight.getBoundingClientRect()
    : undefined
  const target = selected.offsetLeft
  const width = selected.offsetWidth
  const from = frame ? frame.left - track.getBoundingClientRect().left - track.clientLeft : target
  const travel = target - from
  glideMotion?.cancel()
  shineMotion?.cancel()
  highlight.style.setProperty('--nav-highlight-x', `${target}px`)
  highlight.style.width = `${width}px`
  if (!frame || Math.abs(travel) < 1) return

  const direction = Math.sign(travel)
  const stretch = Math.min(0.13, Math.abs(travel) / width * 0.075)
  const transformAt = (x: number, scaleX = 1, scaleY = 1) => `translateX(${x}px) scale(${scaleX}, ${scaleY})`
  glideMotion = highlight.animate([
    { transform: transformAt(from, frame.width / width, frame.height / highlight.offsetHeight) },
    { transform: transformAt(from + travel * 0.66 - width * stretch * 0.4, 1 + stretch, 0.95), offset: 0.38 },
    { transform: transformAt(target + direction * 2.5, 0.993), offset: 0.82 },
    { transform: transformAt(target) },
  ], { duration: 380, easing: 'cubic-bezier(.2,.66,.34,1)' })
  const shineStops = ['translateX(-108%)', 'translateX(169%)']
  if (direction < 0) shineStops.reverse()
  shineMotion = shine.animate([
    { opacity: 0, transform: shineStops[0] },
    { opacity: 0.42, offset: 0.46 },
    { opacity: 0, transform: shineStops[1] },
  ], { duration: 360, easing: 'ease-out' })
}

const resetNavHighlight = () => placeNavHighlight()
watch(() => workspace.value.key, () => placeNavHighlight(true), { flush: 'post' })

watch([() => route.path, () => route.hash], () => {
  if (contentRegion.value) contentRegion.value.scrollTop = 0
}, { flush: 'post' })

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
  reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)')
  reduceMotion.addEventListener('change', resetNavHighlight)
  if (navTrack.value) {
    navResize = new ResizeObserver(resetNavHighlight)
    navResize.observe(navTrack.value)
  }
  resetNavHighlight()
  timer = window.setInterval(() => {
    if (document.visibilityState !== 'visible') return
    void probe()
  }, 15_000)
})

onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer)
  uninstallSelectA11y?.()
  reduceMotion?.removeEventListener('change', resetNavHighlight)
  navResize?.disconnect()
  glideMotion?.cancel()
  shineMotion?.cancel()
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
    <div class="app-shell" :class="{ 'is-immersive': immersive, 'is-processing': route.name === 'data-processing' }">
      <!-- 全局雾底：所有页面共用（含一张图与流程编排）——浅色/深色各一档，地图画布自己仍是深空底 -->
      <div class="app-backdrop" aria-hidden="true" />
      <div class="main-col">
        <a href="#aegis-main" class="skip-content" @click.prevent="contentRegion?.focus()">跳到主要内容</a>
        <header class="topbar">
          <div class="topbar-main">
            <div class="topbrand" data-testid="brand">
              <span class="brand-logo" aria-hidden="true"></span>
              <span class="topbrand__text">
                <span class="brand-name">AEGIS 山地灾害协同调控平台</span>
                <span class="brand-sub">西藏山地灾害多智能体协同调控</span>
                <span class="brand-compact" aria-hidden="true"><strong>AEGIS</strong><span>山地灾害调控</span></span>
              </span>
            </div>
            <nav ref="navTrack" class="topnav" aria-label="主导航">
              <span ref="navHighlight" class="topnav__highlight" aria-hidden="true">
                <span class="topnav__lens"><span ref="navShine" class="topnav__shine" /></span>
              </span>
              <RouterLink v-for="(item, index) in WORKSPACES" :key="item.key" :to="item.to" custom v-slot="{ href, navigate }">
                <a
                  :href="href"
                  class="topnav__item"
                  :aria-current="workspaceCurrent(item.key, item.to)"
                  :data-testid="item.key === 'assistant' ? 'assistant-entry' : `nav-${item.key}`"
                  @click="navigate"
                >
                  <span class="topnav__number" aria-hidden="true">0{{ index + 1 }}</span>
                  <span>{{ item.label }}</span>
                </a>
              </RouterLink>
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
              <RouterLink
                :to="assistantNav.to"
                class="mobile-assistant"
                :aria-label="urgentCards.length ? `智能助手，${urgentCards.length} 条紧急告警` : '智能助手'"
                data-testid="mobile-assistant"
              >
                <RobotOutlined aria-hidden="true" />
                <span v-if="urgentCards.length" class="mobile-assistant__dot" aria-hidden="true" />
              </RouterLink>
            </div>
          </div>
          <nav v-if="workspace.pages && route.name !== 'map'" class="workspace-nav" :aria-label="workspace.label">
            <RouterLink
              v-for="page in workspace.pages"
              :key="page.to"
              :to="page.to"
              class="workspace-nav__item"
            >
              {{ page.label }}
            </RouterLink>
          </nav>
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

        <nav class="mobile-nav" aria-label="主导航" data-testid="mobile-nav">
          <RouterLink v-for="item in mobileItems" :key="item.key" :to="item.to" custom v-slot="{ href, navigate }">
            <a
              :href="href"
              class="mobile-nav__item"
              :aria-current="workspaceCurrent(item.key, item.to)"
              :aria-label="item.label"
              :data-testid="`mobile-nav-${item.key}`"
              @click="navigate"
            >
              <span class="mobile-nav__icon" aria-hidden="true">
                <component :is="mobileIcons[item.key]" />
              </span>
              <span>{{ item.short }}</span>
            </a>
          </RouterLink>
        </nav>
      </div>
      <!-- 全站悬浮的智能助手：所有页面都有（含沉浸式编排页），位置/开合自己持久化 -->
      <!-- 助手页本身就是全屏会话：再叠一份抽屉会出现两份共享同一会话的时间线 -->
      <AssistantDock
        v-if="route.name && route.name !== 'assistant'"
        :immersive="immersive"
        :avoid-map-bar="route.name === 'map'"
        :keep-overview-visible="route.name === 'dashboard'"
      />
    </div>
  </a-config-provider>
</template>

<style>
.app-shell {
  display: flex;
  /* 外壳定高：顶栏固定，正文区自己滚（侧栏已退役，不再有第二列） */
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  background: var(--aeg-color-page);
  color: var(--aeg-color-text);
}

.topnav {
  grid-area: nav;
  position: relative;
  isolation: isolate;
  justify-self: center;
  align-self: center;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  width: min(100%, 560px);
  min-width: 0;
  gap: 4px;
  padding: 4px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: var(--aeg-radius-pill);
  background: linear-gradient(180deg, var(--aeg-color-overview-panel), var(--aeg-color-sunken));
  box-shadow: inset 0 1px 0 var(--aeg-color-glass-border), var(--aeg-shadow-sm);
}
html[data-theme='dark'] .topnav {
  border-color: color-mix(in srgb, var(--aeg-color-overview-accent) 26%, var(--aeg-color-border));
  background: linear-gradient(
    180deg,
    color-mix(in srgb, var(--aeg-color-overview-accent) 13%, var(--aeg-color-masthead-bg)),
    color-mix(in srgb, var(--aeg-color-brand) 8%, var(--aeg-color-sunken))
  );
}
@media (prefers-reduced-transparency: no-preference) {
  .topnav {
    -webkit-backdrop-filter: blur(12px);
    backdrop-filter: blur(12px);
  }
  html[data-theme='dark'] .topnav {
    background: linear-gradient(
      180deg,
      color-mix(in srgb, var(--aeg-color-overview-accent) 13%, var(--aeg-color-masthead-bg)) 0%,
      color-mix(in srgb, var(--aeg-color-sunken) 86%, transparent) 100%
    );
  }
}
.topnav__highlight {
  position: absolute;
  z-index: -1;
  inset: 4px auto 4px 0;
  pointer-events: none;
  transform-origin: left center;
  transform: translateX(var(--nav-highlight-x, 4px));
}
.topnav__lens {
  position: relative;
  display: block;
  height: 100%;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--aeg-color-brand) 43%, var(--aeg-color-border));
  border-radius: var(--aeg-radius-pill);
  background: linear-gradient(115deg, var(--aeg-color-overview-panel), var(--aeg-color-brand-soft)),
    var(--aeg-color-overview-panel);
  box-shadow: inset 0 1px 0 var(--aeg-color-glass-border), 0 2px 10px var(--aeg-color-brand-soft);
}
html[data-theme='dark'] .topnav__lens {
  border-color: color-mix(in srgb, var(--aeg-color-overview-accent) 75%, var(--aeg-color-border));
  background: linear-gradient(
    115deg,
    color-mix(in srgb, var(--aeg-color-overview-accent) 29%, var(--aeg-color-sunken)),
    color-mix(in srgb, var(--aeg-color-brand) 32%, var(--aeg-color-sunken))
  );
  box-shadow:
    inset 0 1px 0 color-mix(in srgb, var(--aeg-color-text) 32%, transparent),
    0 2px 12px var(--aeg-color-brand-soft);
}
.topnav__shine {
  position: absolute;
  inset: 0 auto 0 0;
  width: 62%;
  opacity: 0;
  background: linear-gradient(105deg, transparent, var(--aeg-color-glass-border) 51%, transparent);
}
.topnav__item {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--aeg-space-s2);
  min-width: 0;
  min-height: 40px;
  padding-inline: var(--aeg-space-s2);
  border-radius: var(--aeg-radius-pill);
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-base);
  font-weight: var(--aeg-weight-semibold);
  text-decoration: none;
  white-space: nowrap;
  transition:
    background-color var(--aeg-motion-fast) ease,
    color var(--aeg-motion-base) ease,
    transform var(--aeg-motion-base) cubic-bezier(.22,.74,.34,1);
}
.topnav__item:hover,
.topnav__item[aria-current] {
  color: var(--aeg-color-text);
}
.topnav__item:not([aria-current]):hover {
  background: var(--aeg-color-brand-soft);
  transform: translateY(-1px);
}
.topnav__item:active,
.topnav__item:not([aria-current]):hover:active {
  transform: translateY(1px) scale(.97);
}
.topnav__number {
  display: grid;
  flex: 0 0 23px;
  place-items: center;
  height: 23px;
  border: 1px solid var(--aeg-color-border-strong);
  border-radius: 50%;
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-text-secondary);
  font: var(--aeg-weight-semibold) 11px/1 var(--aeg-font-mono);
  font-variant-numeric: tabular-nums;
  transition: background-color var(--aeg-motion-base) ease, color var(--aeg-motion-base) ease;
}
.topnav__item[aria-current] .topnav__number {
  border-color: var(--aeg-color-masthead-tab);
  background: var(--aeg-color-masthead-tab);
  color: var(--aeg-color-masthead-tab-ink);
}
.topnav__item:focus-visible,
.workspace-nav__item:focus-visible,
.mobile-assistant:focus-visible {
  outline: 2px solid var(--aeg-color-brand);
  outline-offset: -2px;
}
@media (prefers-reduced-motion: reduce) {
  .topnav__item,
  .topnav__number {
    transition: none;
  }
  .topnav__item:active,
  .topnav__item:not([aria-current]):hover,
  .topnav__item:not([aria-current]):hover:active {
    transform: none;
  }
}
@media (forced-colors: active) {
  .topnav__item[aria-current] {
    outline: 1px solid Highlight;
    outline-offset: -2px;
  }
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
.brand-compact {
  display: none;
}

.main-col {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
  height: 100%;
}
.topbar {
  position: relative;
  z-index: 1;
  isolation: isolate;
  flex: none;
  border-bottom: 1px solid var(--aeg-color-border);
  background: var(--aeg-color-masthead-bg);
}
.topbar-main {
  display: grid;
  grid-template: 'brand nav facts' 68px / minmax(240px, 1fr) minmax(435px, 560px) minmax(0, 1fr);
  align-items: center;
  gap: var(--aeg-space-s4);
  padding-inline: var(--aeg-space-s6);
}
.workspace-nav {
  display: flex;
  gap: var(--aeg-space-s5);
  padding-inline: var(--aeg-space-s6);
  border-top: 1px solid var(--aeg-color-border);
}
.workspace-nav__item {
  display: grid;
  place-items: center;
  min-height: 40px;
  padding: 0 var(--aeg-space-s2);
  border-bottom: 2px solid transparent;
  color: var(--aeg-color-text-secondary);
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  text-decoration: none;
  white-space: nowrap;
}
.workspace-nav__item:hover,
.workspace-nav__item[aria-current] {
  color: var(--aeg-color-text);
}
.workspace-nav__item[aria-current] {
  border-color: var(--aeg-color-masthead-tab);
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

.mobile-nav,
.mobile-assistant {
  display: none;
}
@media (max-width: 1219px) {
  .topbar-main {
    grid-template: 'brand facts' 50px 'nav nav' 54px / minmax(0, 1fr) auto;
    gap: 0 var(--aeg-space-s3);
  }
}
/* 窄屏状态用圆点展示，保留读屏文案。 */
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
@media (max-width: 640px) {
  .is-processing .assistant-dock:not(.is-open) {
    display: none;
  }
  .app-shell {
    --aeg-bottom-nav-height: calc(60px + env(safe-area-inset-bottom, 0px));
    box-sizing: border-box;
    padding-top: env(safe-area-inset-top, 0px);
    padding-inline: env(safe-area-inset-left, 0px) env(safe-area-inset-right, 0px);
  }
  .topbar-main {
    grid-template: 'brand facts' 54px / minmax(0, 1fr) auto;
  }
  .topnav {
    display: none;
  }
  .mobile-nav {
    position: relative;
    z-index: 2;
    display: grid;
    flex: 0 0 var(--aeg-bottom-nav-height);
    grid-template-columns: repeat(3, minmax(0, 1fr));
    align-items: stretch;
    padding: 3px var(--aeg-space-s1) calc(3px + env(safe-area-inset-bottom, 0px));
    border-top: 1px solid var(--aeg-color-border-strong);
    background: var(--aeg-color-masthead-bg);
    box-shadow: 0 -4px 16px var(--aeg-color-glass-border);
  }
  .mobile-nav__item {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 2px;
    min-width: 0;
    min-height: 48px;
    border-radius: var(--aeg-radius-md);
    color: var(--aeg-color-text-secondary);
    font-size: var(--aeg-font-xs);
    font-weight: var(--aeg-weight-semibold);
    line-height: var(--aeg-lh-tight);
    text-decoration: none;
    white-space: nowrap;
    -webkit-tap-highlight-color: transparent;
  }
  .mobile-nav__icon {
    position: relative;
    display: grid;
    place-items: center;
    width: min(42px, 100%);
    height: 27px;
    border-radius: var(--aeg-radius-pill);
    font-size: 19px;
    line-height: 1;
    transition:
      transform var(--aeg-motion-base) cubic-bezier(.22,.74,.34,1),
      background-color var(--aeg-motion-base) ease;
  }
  .mobile-nav__item[aria-current],
  .mobile-nav__item:hover {
    color: var(--aeg-color-brand-hover);
  }
  .mobile-nav__item[aria-current] .mobile-nav__icon {
    background: var(--aeg-color-brand-soft);
    box-shadow: inset 0 0 0 1px var(--aeg-color-glass-border);
  }
  .mobile-nav__item:focus-visible {
    outline: 2px solid var(--aeg-color-brand);
    outline-offset: -2px;
  }
  .mobile-assistant {
    position: relative;
    display: grid;
    flex: 0 0 44px;
    place-items: center;
    height: 44px;
    border: 1px solid var(--aeg-color-border);
    border-radius: var(--aeg-radius-md);
    background: var(--aeg-color-sunken);
    color: var(--aeg-color-text-secondary);
    font-size: 20px;
  }
  .mobile-assistant:hover,
  .mobile-assistant[aria-current] {
    border-color: var(--aeg-color-masthead-tab);
    background: var(--aeg-color-brand-soft);
    color: var(--aeg-color-text);
  }
  .mobile-assistant__dot {
    position: absolute;
    top: 6px;
    right: 6px;
    width: 8px;
    height: 8px;
    border: 1px solid var(--aeg-color-masthead-bg);
    border-radius: var(--aeg-radius-pill);
    background: var(--aeg-color-status-danger);
  }
  .brand-sub {
    display: none;
  }
  .brand-name {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  .brand-compact {
    display: grid;
    gap: 2px;
    font-size: 11px;
    white-space: nowrap;
    color: var(--aeg-color-text-secondary);
  }
  .brand-compact strong {
    color: var(--aeg-color-text);
    font-family: var(--aeg-font-display);
    font-size: var(--aeg-font-base);
    letter-spacing: .035em;
  }
  .topbar-main,
  .workspace-nav,
  .main-content,
  .main-content.is-desk {
    padding-inline: var(--aeg-space-s3);
  }
  .main-content.is-desk > :first-child {
    min-height: 100%;
    margin-bottom: var(--aeg-space-s5);
  }
  .topbar-main,
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
  @media (prefers-reduced-motion: no-preference) {
    .mobile-nav__item:active .mobile-nav__icon {
      transform: scale(.9);
    }
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
