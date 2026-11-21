/**
 * 主题模式：`light` / `dark` / `system` 三态，选择持久化到 localStorage。
 *
 * 默认是 `dark`（2026-11-21 指挥中心裁决：本平台是深海军蓝的大屏指挥舱，
 * 首屏就应该是任务书指定的深蓝波浪底，而不是跟随系统落到浅灰）。
 * 老用户显式存过的 `light` / `system` 仍然压过默认值，顶栏切换器三态能力不变。
 */

import { computed, ref, watch } from 'vue'

import { applyTheme } from './vars'
import type { ThemeMode } from './tokens'

export type ThemePreference = ThemeMode | 'system'

const STORAGE_KEY = 'aegis.theme'

function readStored(): ThemePreference {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw === 'light' || raw === 'dark' || raw === 'system' ? raw : 'dark'
  } catch {
    // 隐私模式/禁用存储时 localStorage 会抛：不能因此白屏，退回指挥中心默认深色
    return 'dark'
  }
}

function systemPrefersDark(): boolean {
  return typeof matchMedia === 'function' && matchMedia('(prefers-color-scheme: dark)').matches
}

const mode = ref<ThemePreference>(readStored())

/**
 * 系统偏好必须是 ref 而不是在 computed 里直接调 `matchMedia`：
 * 后者读不到任何响应式依赖，computed 会把它**永久缓存**成第一次的值——
 * 于是"跟随系统"这一档在用户真的翻进制胜/暗色之后毫无反应（写这行时被用例抓到）。
 * 翻系的动作由 `initTheme` 注册的 change 监听写回这个 ref，`resolved` 才跟着重算。
 */
const systemDark = ref(systemPrefersDark())
const resolved = computed<ThemeMode>(() => (mode.value === 'system' ? (systemDark.value ? 'dark' : 'light') : mode.value))

/** 系统偏好变化时，只有"跟随系统"这一档才跟着动；用户显式选过的不动。 */
export function initTheme(): ThemeMode {
  if (typeof matchMedia === 'function') {
    const query = matchMedia('(prefers-color-scheme: dark)')
    if (typeof query.addEventListener === 'function') {
      query.addEventListener('change', (event: MediaQueryListEvent) => {
        systemDark.value = event.matches
      })
    }
  }
  applyTheme(resolved.value)
  return resolved.value
}

export function setMode(next: ThemePreference): void {
  mode.value = next
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    /* 存不下也要立刻生效，只是下次进来不记得 */
  }
  applyTheme(resolved.value)
}

watch(resolved, (next) => applyTheme(next))

export function useTheme() {
  return { mode, resolved, setMode }
}
