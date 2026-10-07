<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import type { ThemeConfig } from 'ant-design-vue/es/config-provider/context'
import zhCN from 'ant-design-vue/es/locale/zh_CN'

import api from '@/api/client'
import { useEventStream } from '@/composables/useEventStream'
import { antdTheme } from '@/theme/antd'
import { useTheme } from '@/theme/useTheme'

const route = useRoute()
const menuKey = computed(() => (route.name as string) ?? 'dashboard')

const pages = [
  { key: 'dashboard', to: '/dashboard', label: '态势总览' },
  { key: 'monitor', to: '/monitor', label: '监测预警' },
  { key: 'warnings', to: '/warnings', label: '预警发布' },
  { key: 'map', to: '/map', label: '一张图' },
  { key: 'workflow', to: '/workflow', label: '流程编排' },
  { key: 'metrics', to: '/metrics', label: '指标量测' },
  { key: 'assistant', to: '/assistant', label: '智能助手' },
] as const

const currentIndex = computed(() => {
  const idx = pages.findIndex((page) => page.key === menuKey.value)
  return idx === -1 ? 0 : idx
})
const pageBadge = computed(
  () => `${String(currentIndex.value + 1).padStart(2, '0')} / ${String(pages.length).padStart(2, '0')}`,
)

const health = ref<{ status: string; version: string } | null>(null)
const { events, connected, stalled } = useEventStream()

let timer: number | undefined

async function probe(): Promise<void> {
  try {
    health.value = await api.health()
  } catch {
    health.value = null
  }
}

onMounted(() => {
  void probe()
  timer = window.setInterval(() => void probe(), 15_000)
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
      <nav class="navbar">
        <div class="nav-brand">
          <router-link class="brand-link" to="/dashboard">
            <span class="brand-logo" aria-hidden="true">A</span>
            <span class="brand-name">AEGIS</span>
          </router-link>
          <span class="brand-sub">山地灾害协同调控</span>
        </div>

        <div class="nav-center">
          <div class="page-badge">{{ pageBadge }}</div>
          <div class="immersive-stepper">
            <router-link
              v-for="(page, idx) in pages"
              :key="page.key"
              class="flow-step"
              :class="{ active: menuKey === page.key }"
              :to="page.to"
              :title="page.label"
              :aria-label="page.label"
              :aria-current="menuKey === page.key ? 'page' : undefined"
            >
              <span class="flow-step-node">{{ idx + 1 }}</span>
              <span class="flow-step-label">{{ page.label }}</span>
            </router-link>
          </div>
        </div>

        <!-- 右列是**一枚**胶囊里装三条真话（min-width 180 / padding 8px 12px / 999 圆角）。
             此前是三枚独立胶囊，@1440 就装不下：在线文案 316px、故障文案 374px，都大于 295px 的列宽，
             只能靠省略号截断（真机截图里 "后端在线 v0…" / "事件流已…"）。合成一枚后 273px / 285px，两档都放得下。
             版本号挪进 title（现场支持要的是 hover 能看到，不是永远占 51px）。 -->
        <div class="nav-right">
          <div class="nav-status" :title="statusTitle">
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

          <!-- 主题切换器：三档都在明面上（不做成"点一下翻一档"的循环钮——汇报现场没人知道要点几次）。
               `aria-pressed` 给读屏，选中的那一格用 `elevated` 底 + 主色字，深色主题下同一套写法成立。 -->
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
      </nav>

      <main class="main-content">
        <router-view />
        <!-- 页脚跟着内容走，不钉在视口底：钉着的话每一页都永久吃掉一条，
             而编排页那种"占满一屏"的布局还要去猜它的高度 -->
        <footer class="footer">
          AEGIS · Adaptive Emergency Geo-hazard Intelligence System ｜ 西藏山地灾害多智能体协同调控技术与平台
        </footer>
      </main>
    </div>
  </a-config-provider>
</template>

<style>
.app-shell {
  display: flex;
  flex-direction: column;
  /* 外壳定高、正文自己滚：原来写 min-height 时正文高度不定，编排页那种
     "三列占满一屏"的布局只能去猜 calc(100vh - 220px)，横幅加进来之后那个 220
     就不成立了——真机量到实例列表最后几行直接盖在页脚上（6 处重叠）。 */
  height: 100vh;
  overflow: hidden;
  background: var(--aeg-color-page);
  color: var(--aeg-color-text);
}

/* —— 顶栏 ——
   上一版是"半透白 + blur(22px) saturate(1.25) + 两道渐变底 + 蓝雾投影"。改成不透明 `card` 底 +
   一条 `border` 分隔：顶栏下面是会滚动的表格与 Cesium 画面，模糊那一层既贵（每帧重算）又让描边发灰。
   深色主题下它自动变深蓝黑，不需要第二套写法。 */
.navbar {
  position: sticky;
  top: 0;
  z-index: var(--aeg-z-header);
  /* 三列网格：左右两列都是 1fr 等宽，因此中间列恒等于屏幕中线；
     窄屏时先压缩/隐藏两侧内容，而不是像绝对居中那样让三组文字互相压住。 */
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  gap: var(--aeg-space-s4);
  height: var(--aeg-header-height);
  padding: 0 var(--aeg-space-s6);
  background: var(--aeg-color-card);
  border-bottom: 1px solid var(--aeg-color-border);
  box-shadow: var(--aeg-shadow-sm);
}

.nav-brand {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s3);
  justify-self: start;
  min-width: 0;
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
  padding-left: var(--aeg-space-s3);
  border-left: 1px solid var(--aeg-color-border);
  font-size: var(--aeg-font-xs);
  color: var(--aeg-color-text-secondary);
  white-space: nowrap;
}

/* —— 中间：页码徽标 + 步进器 ——
   步进器改成"轨道 + 选中格"的分段控件写法（Arco/Linear 那一族）：轨道 `sunken`、选中格 `elevated` + 一档投影，
   不再是渐变蓝实心 + 蓝雾投影。页码徽标也从实心渐变换成中性胶囊——它是读数不是操作，不该比操作更显眼。 */
.nav-center {
  display: flex;
  align-items: center;
  gap: var(--aeg-space-s4);
  justify-self: center;
  min-width: 0;
}
.page-badge {
  padding: 5px var(--aeg-space-s3);
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-sunken);
  color: var(--aeg-color-text-secondary);
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-bold);
  letter-spacing: 0.05em;
  white-space: nowrap;
}
.immersive-stepper {
  display: flex;
  align-items: center;
  padding: 6px;
  border: 1px solid var(--aeg-color-border);
  border-radius: var(--aeg-radius-pill);
  background: var(--aeg-color-sunken);
}
.flow-step {
  position: relative;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 12px 6px 8px;
  border-radius: var(--aeg-radius-pill);
  color: var(--aeg-color-text-secondary);
  text-decoration: none;
  white-space: nowrap;
  transition: background-color var(--aeg-motion-base) ease, color var(--aeg-motion-base) ease, box-shadow var(--aeg-motion-base) ease;
}
.flow-step + .flow-step::before {
  content: '';
  position: absolute;
  left: -5px;
  width: 10px;
  height: 1px;
  background: var(--aeg-color-border-strong);
}
.flow-step-node {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--aeg-color-card);
  color: var(--aeg-color-text-secondary);
  font-family: var(--aeg-font-mono);
  font-size: var(--aeg-font-xs);
  font-weight: var(--aeg-weight-bold);
  transition: background-color var(--aeg-motion-base) ease, color var(--aeg-motion-base) ease;
}
.flow-step-label {
  font-size: var(--aeg-font-sm);
  font-weight: var(--aeg-weight-semibold);
  line-height: 18px;
}
.flow-step:not(.active):hover {
  background: var(--aeg-color-brand-soft);
  color: var(--aeg-color-text);
}
.flow-step.active {
  background: var(--aeg-color-elevated);
  box-shadow: var(--aeg-shadow-sm);
  color: var(--aeg-color-text);
}
.flow-step.active .flow-step-node {
  /* 选中格的编号是实心主色 + `text-inverse`（浅主题白字 5.98:1、深主题深蓝黑字 5.09:1，两档都过 AA）。
     上一版这里挂的是"白 22% 蒙在渐变蓝上 + 白字"，真机量到 11px 编号只有 3.49:1。 */
  background: var(--aeg-color-brand);
  color: var(--aeg-color-text-inverse);
}

/* —— 右侧：状态胶囊 + 主题切换器 —— */
.nav-right {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  justify-self: end;
  gap: var(--aeg-space-s3);
  min-width: 0;
  max-width: 100%;
}
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
/* 挤不下时省略号收尾（全称在 title 里），不许无声裁字、更不许顶开步进器 */
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

/* 主题切换器：同一族"轨道 + 选中格"的分段写法，与步进器一眼是一类控件。
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

/* 窄屏逐层收：先收副题，再收步进器文字（编号点与顺序仍在，页名挪进 title/aria-label），最后收状态文字。
   三档都是"少显示一点"，不是"互相压上去"也不是"把状态胶囊裁掉半颗"——
   真机量到的顶栏重叠与静默裁切就是这么来的。

   步进器文字的下限从 1320 提到 1640 是这一轮量出来的（探针读网格的**已用轨道宽**）：
   七格带名字时中间列占 773px，两侧轨道只剩 (W−853)/2 —— @1440 是 293px、@1366 只有 256px；
   而右列现在要装"状态胶囊 273 + 间距 12 + 主题切换器 90 = 375px"（三个数都是量的）。
   轨道不够时胶囊会被 flex 挤到 scrollWidth > clientWidth，那是**静默裁字**（那条判据本来就是禁它的）。
   轨道 ≥375 要 W ≥ 1600，所以带名字的档位从 1600 起（留 40px 余量取 1640）：
   1920 大屏铺页名，1440/1366 笔记本收成编号点。 */
@media (max-width: 1360px) {
  .brand-sub {
    display: none;
  }
}
@media (max-width: 1640px) {
  .flow-step-label {
    display: none;
  }
  .flow-step {
    padding: 6px 8px;
  }
  .nav-center {
    gap: var(--aeg-space-s3);
  }
}
@media (max-width: 1140px) {
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
