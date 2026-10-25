/**
 * 令牌 → CSS 变量：运行时注入，不另存一份 `vars.css`。
 *
 * 为什么运行时注入而不是生成一个 CSS 文件：生成物一旦落盘就有了第二真源，
 * 谁手改 CSS 或忘了重跑生成，令牌就与页面分叉（这类"两张皮"本项目已吃过几次）。
 * 注入发生在 `app.mount()` 之前（`main.ts`），所以首屏就有变量可解析，不会闪白。
 */

import { colorSlots, font, fontFamily, layout, motion, radius, riskText, shadow, space, tagTone, z, type ColorSlot, type ThemeMode } from './tokens'

export const varPrefix = '--aeg-'

export function colorVars(mode: ThemeMode): Record<string, string> {
  const out: Record<string, string> = {}
  for (const slot of Object.keys(colorSlots[mode]) as ColorSlot[]) out[`${varPrefix}color-${slot}`] = colorSlots[mode][slot]
  const levels = Object.keys(riskText[mode]) as unknown as (keyof typeof riskText[typeof mode])[]
  for (const level of levels) out[`${varPrefix}risk-text-${level}`] = riskText[mode][level as 1 | 2 | 3 | 4 | 5]
  for (const [tone, trio] of Object.entries(tagTone[mode])) {
    out[`${varPrefix}tag-${tone}-bg`] = trio.bg
    out[`${varPrefix}tag-${tone}-line`] = trio.line
    out[`${varPrefix}tag-${tone}-text`] = trio.text
  }
  return out
}

/** 与主题无关的那几组（间距/字号/圆角/动效/层级/布局尺寸） */
export function staticVars(): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(space)) out[`${varPrefix}space-${k}`] = `${v}px`
  for (const [k, v] of Object.entries(font.size)) out[`${varPrefix}font-${k}`] = `${v}px`
  for (const [k, v] of Object.entries(font.weight)) out[`${varPrefix}weight-${k}`] = `${v}`
  for (const [k, v] of Object.entries(font.lineHeight)) out[`${varPrefix}lh-${k}`] = `${v}`
  for (const [k, v] of Object.entries(radius)) out[`${varPrefix}radius-${k}`] = v === 999 ? '999px' : `${v}px`
  // 玻璃卡的背景模糊量：只在这一处定义，卡片们引 var，别各写一个 blur(18px)
  out[`${varPrefix}glass-blur`] = '18px'
  for (const [k, v] of Object.entries(motion)) out[`${varPrefix}motion-${k}`] = v
  for (const [k, v] of Object.entries(z)) out[`${varPrefix}z-${k}`] = `${v}`
  out[`${varPrefix}header-height`] = `${layout.headerHeight}px`
  out[`${varPrefix}editor-header-height`] = `${layout.editorHeaderHeight}px`
  out[`${varPrefix}editor-panel-width`] = `${layout.editorPanelWidth}px`
  out[`${varPrefix}node-width`] = `${layout.nodeWidth}px`
  out[`${varPrefix}sidebar-width`] = `${layout.sidebarWidth}px`
  out[`${varPrefix}sidebar-collapsed`] = `${layout.sidebarCollapsed}px`
  out[`${varPrefix}content-max`] = `${layout.contentMaxWidth}px`
  out[`${varPrefix}font-sans`] = fontFamily.sans
  out[`${varPrefix}font-mono`] = fontFamily.mono
  return out
}

export function themeVars(mode: ThemeMode): Record<string, string> {
  const colors = colorVars(mode)
  const shadows = {
    [`${varPrefix}shadow-sm`]: shadow[mode].sm,
    [`${varPrefix}shadow-md`]: shadow[mode].md,
    [`${varPrefix}shadow-lg`]: shadow[mode].lg,
  }
  return { ...staticVars(), ...colors, ...shadows }
}

/** 写进 documentElement；`data-theme` 同时给 CSS 选择器用（`html[data-theme='dark'] …`）。 */
export function applyTheme(mode: ThemeMode, root: HTMLElement | null = document.documentElement): void {
  if (!root) return
  const vars = themeVars(mode)
  for (const [name, value] of Object.entries(vars)) root.style.setProperty(name, value)
  root.dataset.theme = mode
  root.style.colorScheme = mode
}
