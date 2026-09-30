import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/** 路由级懒加载：首屏只加载态势页，其余按访问时机分块。 */
const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/dashboard' },
  {
    path: '/dashboard',
    name: 'dashboard',
    component: () => import('./views/DashboardView.vue'),
    meta: { title: '态势总览' },
  },
  {
    path: '/monitor',
    name: 'monitor',
    component: () => import('./views/MonitorView.vue'),
    meta: { title: '监测预警' },
  },
  {
    path: '/warnings',
    name: 'warnings',
    component: () => import('./views/WarningsView.vue'),
    meta: { title: '预警发布' },
  },
  {
    path: '/map',
    name: 'map',
    component: () => import('./views/MapView.vue'),
    meta: { title: '一张图' },
  },
  {
    path: '/workflow',
    name: 'workflow',
    component: () => import('./views/WorkflowView.vue'),
    meta: { title: '流程编排' },
  },
  {
    path: '/metrics',
    name: 'metrics',
    component: () => import('./views/MetricsView.vue'),
    meta: { title: '指标量测' },
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
