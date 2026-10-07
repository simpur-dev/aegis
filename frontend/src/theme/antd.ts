/**
 * 组件库主题 = 令牌派生（"组件库主题从同一令牌派生"这条硬要求落在这里）。
 *
 * 这一份是**唯一**允许把令牌交给 antd 的地方：`App.vue` 不再自己写 hex，
 * 换库时（选型确认后）要改的只有这个文件的函数体，令牌与 CSS 变量那两层不动。
 *
 * 浅色那一档刻意与改造前的 ConfigProvider 取值**逐项相同**（含 hover/active 往深走那两条——
 * 那是悬停态体检量出来的：antd 默认把 hover 派生成更浅的蓝 #3b82f6，白字只有 3.68:1），
 * 这样"接令牌层"这一步不掺任何视觉变化，视觉回归另开批次负责。
 */

import { theme } from 'ant-design-vue'
import type { ThemeConfig } from 'ant-design-vue/es/config-provider/context'

import { brand, colorSlots, fontFamily, motion, radius, type ThemeMode } from './tokens'

export function antdTheme(mode: ThemeMode): ThemeConfig {
  const c = colorSlots[mode]
  return {
    algorithm: mode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token: {
      colorPrimary: c.brand,
      colorInfo: c.brand,
      colorLink: c.brand,
      ...(mode === 'light' ? { colorPrimaryHover: brand.light.hover, colorPrimaryActive: brand.light.active } : {}),
      colorBgLayout: c.page,
      colorBgContainer: c.card,
      colorBgElevated: c.elevated,
      colorFillAlter: c.alter,
      colorBorder: c.border,
      colorBorderSecondary: c['border-subtle'],
      colorText: c.text,
      colorTextDescription: c['text-secondary'],
      borderRadius: radius.md,
      borderRadiusLG: radius.lg,
      fontFamily: fontFamily.sans,
      motionDurationFast: motion.fast,
      motionDurationMid: motion.base,
      motionDurationSlow: motion.slow,
    },
  }
}
