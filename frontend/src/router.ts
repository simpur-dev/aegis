import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/**
 * 主导航唯一真源（2026-10-17 信息架构收并，用户拍板）。
 *
 * 侧栏（App.vue）与本路由表都从这儿取，不再各自抄一份——"路由加了页、导航忘了挂"
 * 与"导航挂了页、路由没有"两类漂移由 `uiTheme.spec.ts` 的对账用例钉住。
 *
 * 分组即受众：`main` 是值班主线（看什么→判什么→做什么），`system` 是运维/验收取证面。
 * 旧入口（/warnings /simulation /assistant）保留可访问但标 `nav: false`：
 * 它们正在按阶段合并进主线页（预警→监测预警页、推演→编排页）或改常驻抽屉（助手），
 * 合并完成前不从主导航消失，只是不再占一级位置。
 */
export interface NavItem {
  key: string
  to: string
  /** 展开态页名（侧栏与读屏都用它） */
  label: string
  /** 收起态短名（侧栏压成 64px 时用） */
  short: string
}

export interface NavGroup {
  key: string
  label: string
  items: NavItem[]
}

export const NAV_GROUPS: NavGroup[] = [
  {
    key: 'main',
    label: '值班主线',
    items: [
      { key: 'dashboard', to: '/dashboard', label: '态势总览', short: '总览' },
      { key: 'monitor', to: '/monitor', label: '监测预警', short: '监测' },
      { key: 'map', to: '/map', label: '一张图', short: '地图' },
      { key: 'workflow', to: '/workflow', label: '流程编排', short: '编排' },
    ],
  },
  {
    key: 'system',
    label: '系统',
    items: [{ key: 'metrics', to: '/metrics', label: '指标量测', short: '指标' }],
  },
]

/** 路由级懒加载：首屏只加载态势页，其余按访问时机分块。 */
const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/dashboard' },
  {
    path: '/dashboard',
    name: 'dashboard',
    component: () => import('./views/DashboardView.vue'),
    meta: { title: '态势总览', nav: 'main' },
  },
  {
    path: '/monitor',
    name: 'monitor',
    component: () => import('./views/MonitorView.vue'),
    meta: { title: '监测预警', nav: 'main' },
  },
  {
    path: '/warnings',
    name: 'warnings',
    component: () => import('./views/WarningsView.vue'),
    meta: { title: '预警发布', nav: false },
  },
  {
    path: '/map',
    name: 'map',
    component: () => import('./views/MapView.vue'),
    meta: { title: '一张图', nav: 'main' },
  },
  {
    path: '/workflow',
    name: 'workflow',
    component: () => import('./views/WorkflowView.vue'),
    meta: { title: '流程编排', nav: 'main' },
  },
  {
    path: '/metrics',
    name: 'metrics',
    component: () => import('./views/MetricsView.vue'),
    meta: { title: '指标量测', nav: 'system' },
  },
  {
    path: '/simulation',
    name: 'simulation',
    component: () => import('./views/SimulationView.vue'),
    meta: { title: '推演沙盒', nav: false },
  },
  {
    path: '/assistant',
    name: 'assistant',
    component: () => import('./views/AssistantView.vue'),
    meta: { title: '智能助手', nav: false },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  const title = (to.meta?.title as string) ?? 'AEGIS'
  document.title = `${title} · AEGIS 山地灾害协同调控平台`
})
