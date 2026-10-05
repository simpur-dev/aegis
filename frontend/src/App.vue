<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import type { ThemeConfig } from 'ant-design-vue/es/config-provider/context'
import zhCN from 'ant-design-vue/es/locale/zh_CN'

import api from '@/api/client'
import { useEventStream } from '@/composables/useEventStream'

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
 * 全局设计令牌（对标 NexusMind）：科技蓝主色、浅灰蓝底、白卡、Space Grotesk
 * 拉丁 + 系统中文栈；令牌管不到的整型规则在 styles/theme.css。
 */
const theme: ThemeConfig = {
  token: {
    colorPrimary: '#2563EB',
    colorInfo: '#2563EB',
    colorLink: '#2563EB',
    colorBgLayout: '#F2F4F8',
    colorBgContainer: '#FFFFFF',
    colorFillAlter: '#F7F9FC',
    colorBorder: '#E6E8EF',
    colorBorderSecondary: '#EEF0F5',
    colorText: '#0A0A0C',
    colorTextDescription: '#5A6072',
    borderRadius: 10,
    borderRadiusLG: 16,
    fontFamily:
      "'Space Grotesk', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', 'Noto Sans SC', sans-serif",
  },
}

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
            >
              <span class="flow-step-node">{{ idx + 1 }}</span>
              <span class="flow-step-label">{{ page.label }}</span>
            </router-link>
          </div>
        </div>

        <div class="nav-status">
          <span class="status-pill" :title="health ? `后端在线 v${health.version}` : '后端不可达：取数与操作都会失败'">
            <span class="status-dot" :class="health ? 'ok' : 'bad'"></span>
            <span class="status-pill__text">{{ health ? `后端在线 v${health.version}` : '后端不可达' }}</span>
          </span>
          <span class="status-pill" data-testid="stream-badge" :title="streamHint">
            <span class="status-dot" :class="connected && health ? 'live' : 'bad'"></span>
            <span class="status-pill__text">{{ streamLabel }}</span>
          </span>
          <span class="status-pill" title="最近事件">
            <span class="events-count">{{ events.length }}</span>
            <span>事件</span>
          </span>
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
  background: #f2f4f8;
  color: #1d2129;
}

/* —— 顶栏（对标 NexusMind 的 Process 页导航）—— */
.navbar {
  position: sticky;
  top: 0;
  z-index: 100;
  /* 三列网格：左右两列都是 1fr 等宽，因此中间列恒等于屏幕中线；
     窄屏时先压缩/隐藏两侧内容，而不是像绝对居中那样让三组文字互相压住。 */
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  gap: 16px;
  height: 68px;
  padding: 0 24px;
  background:
    linear-gradient(90deg, rgba(255, 255, 255, 0.92), rgba(240, 247, 255, 0.86)),
    radial-gradient(circle at 28% 0%, rgba(37, 99, 235, 0.1), transparent 38%);
  backdrop-filter: blur(22px) saturate(1.25);
  border-bottom: 1px solid rgba(37, 99, 235, 0.12);
  box-shadow: 0 8px 26px rgba(29, 33, 41, 0.05);
}

.nav-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  justify-self: start;
  min-width: 0;
}
.brand-link {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  text-decoration: none;
}
.brand-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 10px;
  background: linear-gradient(135deg, #2563eb, #1d4ed8);
  box-shadow: 0 8px 18px rgba(37, 99, 235, 0.24);
  color: #fff;
  font-size: 16px;
  font-weight: 800;
}
.brand-name {
  font-size: 16px;
  font-weight: 800;
  letter-spacing: 0.02em;
  color: #0b1220;
}
.brand-sub {
  padding-left: 10px;
  border-left: 1px solid rgba(78, 89, 105, 0.18);
  font-size: 11px;
  color: #5a6072;
  white-space: nowrap;
}

/* —— 中间：页码徽标 + 沉浸式胶囊步进器 —— */
.nav-center {
  display: flex;
  align-items: center;
  gap: 14px;
  justify-self: center;
  min-width: 0;
}
.page-badge {
  padding: 5px 12px;
  border-radius: 999px;
  background: linear-gradient(135deg, #1d4ed8, #2563eb);
  box-shadow: 0 8px 20px rgba(37, 99, 235, 0.18);
  color: #fff;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.05em;
  white-space: nowrap;
}
.immersive-stepper {
  display: flex;
  align-items: center;
  padding: 6px;
  border: 1px solid rgba(37, 99, 235, 0.12);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.78);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.86),
    0 8px 22px rgba(29, 33, 41, 0.04);
}
.flow-step {
  position: relative;
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 12px 6px 8px;
  border-radius: 999px;
  color: #5a6072;
  text-decoration: none;
  white-space: nowrap;
  transition: all 0.24s ease;
}
.flow-step + .flow-step::before {
  content: '';
  position: absolute;
  left: -5px;
  width: 10px;
  height: 1px;
  background: rgba(78, 89, 105, 0.18);
}
.flow-step-node {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: rgba(242, 243, 245, 0.9);
  color: #5a6072;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  font-weight: 800;
  transition: all 0.24s ease;
}
.flow-step-label {
  font-size: 12px;
  font-weight: 600;
  line-height: 18px;
}
.flow-step:not(.active):hover {
  background: rgba(37, 99, 235, 0.08);
}
.flow-step.active {
  background: linear-gradient(135deg, #2563eb, #1d4ed8);
  box-shadow: 0 8px 22px rgba(37, 99, 235, 0.22);
  color: #fff;
}
.flow-step.active .flow-step-node {
  /* 原先是"白 22% 蒙在渐变蓝上 + 白字"，真机量到 11px 编号只有 3.49:1；
     换成实心白底 + 深蓝编号，选中的立体感靠投影而不是透明度维持。 */
  background: #fff;
  box-shadow: 0 2px 8px rgba(13, 30, 66, 0.22);
  color: #1d4ed8;
}

/* —— 右侧：状态胶囊 —— */
.nav-status {
  display: flex;
  align-items: center;
  gap: 8px;
  justify-self: end;
  min-width: 0;
  /* 右列此前会往左长出轨道之外（真机 @1440 故障态：内容 374px 撞进 295px 的列，
     压住步进器第 7 档 78px）。胶囊是 nowrap 的，flex 项默认 min-width:auto，
     于是整列的 min-content 比轨道还宽、`1fr` 也压不住——得让胶囊自己可缩。 */
  max-width: 100%;
  overflow: hidden;
}
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  flex: 0 1 auto;
  min-width: 0;
  padding: 6px 12px;
  border: 1px solid rgba(37, 99, 235, 0.12);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.56);
  color: #4e5969;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}
/* 挤不下时省略号收尾（完整那句在 title 里），不许无声裁字、更不许顶开步进器 */
.status-pill__text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.status-dot {
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: rgba(115, 168, 185, 0.4);
}
.status-dot.ok {
  background: #14b8a6;
  box-shadow: 0 0 10px rgba(20, 184, 166, 0.36);
}
.status-dot.live {
  background: #2563eb;
  box-shadow: 0 0 10px rgba(37, 99, 235, 0.38);
  animation: status-pulse 1.5s infinite;
}
.status-dot.bad {
  background: #ef4444;
  box-shadow: 0 0 10px rgba(239, 68, 68, 0.8);
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
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
  color: #0b1220;
}

.main-content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 20px 24px;
}
.footer {
  padding: 16px;
  background: transparent;
  text-align: center;
  font-size: 12px;
  color: #5a6072;
}

/* 窄屏逐层收：先收副题，再收步进器文字（编号点与顺序仍在），最后收状态文字。
   三档都是"少显示一点"，不是"互相压上去"也不是"把状态胶囊裁掉半颗"——
   真机量到的顶栏重叠与静默裁切就是这么来的（1440 全量、1320 起收文字标签）。 */
@media (max-width: 1360px) {
  .brand-sub {
    display: none;
  }
}
@media (max-width: 1320px) {
  .flow-step-label {
    display: none;
  }
  .flow-step {
    padding: 6px 8px;
  }
  .nav-center {
    gap: 10px;
  }
}
@media (max-width: 1140px) {
  .status-pill__text {
    display: none;
  }
  .status-pill {
    padding: 6px 8px;
  }
}
</style>
