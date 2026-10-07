/**
 * 主题三态这一层的用例（阶段 0）。
 *
 * 令牌本身对不对由 `tokens.spec.ts` 管，这份管的是**三态与持久化**：
 * ①默认跟随系统、②用户显式选过的压过系统、③系统偏好真的翻了要跟着翻。
 * 第三档不是纸上推理——写第一版时 `resolved` 里直接调 `matchMedia`，computed 把它缓存成
 * 第一次的值，"翻系"事件到了也一动不动；这条用例就是钉那个坑的。
 *
 * `useTheme` 是模块级单例（全应用一份状态），所以每个用例都要 `resetModules()` 后重新 import，
 * 并把 localStorage 与 matchMedia 在 import **之前**摆好——首屏读的那一次不会重来。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Ref } from 'vue'

import { colorSlots } from '@/theme/tokens'
import type { ThemeMode } from '@/theme/tokens'
import type { ThemePreference } from '@/theme/useTheme'

type ChangeHandler = (event: { matches: boolean }) => void

let systemMatches = false
let handlers: ChangeHandler[] = []

function installMatchMedia(initial: boolean): void {
  systemMatches = initial
  handlers = []
  vi.stubGlobal('matchMedia', (query: string) => ({
    media: query,
    get matches() {
      return systemMatches
    },
    addEventListener: (_: string, handler: ChangeHandler) => handlers.push(handler),
    removeEventListener: () => {},
  }))
}

/** 模拟用户在操作系统里翻进制胜/浅色：浏览器会派发一条 change 事件。 */
function flipSystem(next: boolean): void {
  systemMatches = next
  for (const handler of handlers) handler({ matches: next })
}

interface ThemeHandle {
  mode: Ref<ThemePreference>
  resolved: Ref<ThemeMode>
  setMode: (next: ThemePreference) => void
  initTheme: () => ThemeMode
}

/** 重新 import 一份模块状态（单例），再取一次钩子——等价于应用启动时的那一次。 */
async function freshTheme(): Promise<ThemeHandle> {
  const mod = await import('@/theme/useTheme')
  const { mode, resolved, setMode } = mod.useTheme()
  return { mode, resolved, setMode, initTheme: mod.initTheme }
}

function applied(name: string): string {
  return document.documentElement.style.getPropertyValue(name).trim()
}

beforeEach(() => {
  localStorage.clear()
  delete document.documentElement.dataset.theme
  document.documentElement.removeAttribute('style')
})

afterEach(() => {
  /* spy 必须显式还原：`vi.spyOn(Storage.prototype,…)` 会留在原型上，
     后面那些"要验持久化"的用例会读到 null，红得毫无道理（本文件踩过一次）。 */
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.resetModules()
})

describe('主题三态与持久化', () => {
  it('没存过偏好就跟随系统：系统暗 → 首屏就是深色，且真的写进 DOM', async () => {
    installMatchMedia(true)
    const theme = await freshTheme()
    expect(theme.mode.value).toBe('system')
    expect(theme.initTheme()).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(applied('--aeg-color-page')).toBe(colorSlots.dark.page)
    expect(document.documentElement.style.colorScheme).toBe('dark')
  })

  it('存过 light 就压过系统：系统暗也必须停在浅色（用户显式选过的大于系统）', async () => {
    localStorage.setItem('aegis.theme', 'light')
    installMatchMedia(true)
    const theme = await freshTheme()
    expect(theme.initTheme()).toBe('light')
    expect(theme.resolved.value).toBe('light')
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(applied('--aeg-color-page')).toBe(colorSlots.light.page)
  })

  it('跟随系统时系统翻暗：页面立刻换肤，不是停在第一次算出来的那档', async () => {
    installMatchMedia(false)
    const theme = await freshTheme()
    const { nextTick } = await import('vue')
    expect(theme.initTheme()).toBe('light')

    flipSystem(true)
    await nextTick()
    expect(theme.resolved.value).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(applied('--aeg-color-card')).toBe(colorSlots.dark.card)

    flipSystem(false)
    await nextTick()
    expect(theme.resolved.value).toBe('light')
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(handlers.length, '没接 change 监听——翻系这件事根本没被订阅').toBeGreaterThanOrEqual(1)
  })

  it('用户已显式选 dark：系统翻回浅色不许把他拽走', async () => {
    installMatchMedia(false)
    const theme = await freshTheme()
    theme.initTheme()
    theme.setMode('dark')
    expect(localStorage.getItem('aegis.theme')).toBe('dark')

    flipSystem(true)
    const { nextTick } = await import('vue')
    await nextTick()
    expect(theme.resolved.value).toBe('dark')
    expect(localStorage.getItem('aegis.theme')).toBe('dark')
  })

  it('setMode 当场生效不当异步：点一下就要看到换肤', async () => {
    installMatchMedia(false)
    const theme = await freshTheme()
    theme.setMode('dark')
    expect(theme.mode.value).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(applied('--aeg-color-text')).toBe(colorSlots.dark.text)

    theme.setMode('system')
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(localStorage.getItem('aegis.theme')).toBe('system')
  })

  it('存的是垃圾值也退回跟随系统，而不是白屏', async () => {
    localStorage.setItem('aegis.theme', 'neon')
    installMatchMedia(true)
    const theme = await freshTheme()
    expect(theme.mode.value).toBe('system')
    expect(theme.initTheme()).toBe('dark')
  })

  it('localStorage 整个不可用（隐私模式）：读不炸、点了照样换肤', async () => {
    installMatchMedia(false)
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('SecurityError')
    })
    const theme = await freshTheme()
    getItem.mockRestore()
    expect(theme.mode.value).toBe('system')

    theme.setMode('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(applied('--aeg-color-border')).toBe(colorSlots.dark.border)
  })
})
