/**
 * 应用装配契约：`main.ts` 必须把每个"运行时必需的插件"装上，且在 mount 之前。
 *
 * 这条门禁的由来是一次真机巡检：/workflow 整页渲染成空白，控制台只有一条 Vue 的组件栈 warn，
 * 真凶是 `main.ts` 少了 `app.use(createPinia())`——`stores/workflow.ts` 被六个画布组件在
 * setup 顶层取用，没有激活的 pinia 就直接抛。而当时 430 项前端测试全绿：
 * **每个 spec 都用 `global.plugins: [createPinia()]` 自己挂了一份**，
 * 于是"组件在测试里能用"被误当成"应用在装配后能用"。
 *
 * 所以这里不去重测组件，只对账装配本身：源码里凡是取用 store 的视图，运行时就必须有 pinia。
 */

import { describe, expect, it } from 'vitest'

import { readRepoFile } from '@/testing/repoSource'

function mainSource(): string {
  return readRepoFile('frontend', 'src', 'main.ts')
}

/** 从源码里取 `const app = createApp(App)` 之后、`app.mount(` 之前的那段装配序列。 */
function wiring(text: string): string {
  const start = text.indexOf('createApp(')
  const end = text.indexOf('app.mount(')
  expect(start).toBeGreaterThanOrEqual(0)
  expect(end).toBeGreaterThanOrEqual(0)
  return text.slice(start, end)
}

describe('main.ts 的插件装配', () => {
  it('pinia 被装上：视图在 setup 顶层取 store，缺它页面直接空白', () => {
    const users = readRepoFile('frontend', 'src', 'views', 'WorkflowView.vue')
    expect(users).toContain('useWorkflowStore()')
    expect(wiring(mainSource())).toMatch(/app\.use\(\s*createPinia\(\)\s*\)/)
  })

  it('router 也在 mount 之前装：否则首屏永远停在空 RouterView', () => {
    expect(wiring(mainSource())).toMatch(/app\.use\(\s*router\s*\)/)
  })

  it('组件库里用到的 antd 由 app.use(Antd) 提供，不靠各文件自行 import 组件', () => {
    expect(wiring(mainSource())).toMatch(/app\.use\(\s*Antd\s*\)/)
  })

  it('装配顺序在 mount 之前（装完再挂，否则首帧取不到插件）', () => {
    const text = mainSource()
    const mount = text.indexOf('app.mount(')
    for (const call of ['app.use(Antd)', 'app.use(createPinia())', 'app.use(router)']) {
      expect(text.indexOf(call), `${call} 出现在 mount 之后`).toBeLessThan(mount)
      expect(text.indexOf(call)).toBeGreaterThanOrEqual(0)
    }
  })
})
