import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

export type WorkspaceKey = 'duty' | 'research' | 'data' | 'assistant'

declare module 'vue-router' {
  interface RouteMeta {
    workspace?: WorkspaceKey
  }
}

interface WorkspaceLink {
  to: string
  label: string
}

interface Workspace extends WorkspaceLink {
  key: WorkspaceKey
  short: string
  pages?: readonly WorkspaceLink[]
}

export const WORKSPACES = [
  {
    key: 'duty',
    label: '值班处置',
    short: '值班',
    to: '/dashboard',
    pages: [
      { to: '/dashboard', label: '态势总览' },
      { to: '/warnings', label: '告警与任务' },
    ],
  },
  { key: 'research', label: '多智能体研究', short: '研究', to: '/workflow' },
  {
    key: 'data',
    label: '数据处理',
    short: '数据',
    to: '/data-processing',
    pages: [
      { to: '/data-processing', label: '处理流水线' },
      { to: '/monitor', label: '监测数据' },
      { to: '/metrics', label: '指标量测' },
    ],
  },
  { key: 'assistant', label: '助手', short: '助手', to: '/assistant' },
] as const satisfies readonly Workspace[]

export function workspaceFor(route: { name?: unknown; hash: string; meta: { workspace?: WorkspaceKey } }): Workspace {
  // 同一监测页面保留演练深链接，演练仍属于值班处置。
  if (route.name === 'monitor' && route.hash === '#drill') return WORKSPACES[0]
  return WORKSPACES.find((workspace) => workspace.key === route.meta.workspace) ?? WORKSPACES[0]
}

/** 路由级懒加载：首屏只加载态势页，其余按访问时机分块。 */
const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/dashboard' },
  {
    path: '/dashboard',
    name: 'dashboard',
    component: () => import('./views/DashboardView.vue'),
    meta: { title: '态势总览', workspace: 'duty' },
  },
  {
    path: '/data-processing',
    name: 'data-processing',
    component: () => import('./views/DataProcessingView.vue'),
    meta: { title: '处理流水线', workspace: 'data' },
  },
  {
    path: '/monitor',
    name: 'monitor',
    component: () => import('./views/MonitorView.vue'),
    meta: { title: '监测数据', workspace: 'data' },
  },
  {
    path: '/warnings',
    name: 'warnings',
    component: () => import('./views/WarningsView.vue'),
    meta: { title: '告警与任务', workspace: 'duty' },
  },
  {
    path: '/map',
    name: 'map',
    component: () => import('./views/MapView.vue'),
    meta: { title: '一张图', workspace: 'duty' },
  },
  {
    path: '/workflow',
    name: 'workflow',
    component: () => import('./views/WorkflowView.vue'),
    meta: { title: '流程编排', workspace: 'research', immersive: true },
  },
  {
    path: '/metrics',
    name: 'metrics',
    component: () => import('./views/MetricsView.vue'),
    meta: { title: '指标量测', workspace: 'data' },
  },
  {
    path: '/simulation',
    name: 'simulation',
    component: () => import('./views/SimulationView.vue'),
    meta: { title: '推演沙盒', workspace: 'research' },
  },
  {
    path: '/assistant',
    name: 'assistant',
    component: () => import('./views/AssistantView.vue'),
    meta: { title: '智能助手', workspace: 'assistant' },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  const title = to.name === 'monitor' && to.hash === '#drill' ? '处置演练' : (to.meta.title as string) ?? 'AEGIS'
  document.title = `${title} · AEGIS 山地灾害协同调控平台`
})
