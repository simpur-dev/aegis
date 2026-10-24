/**
 * 设计令牌单一真源（阶段 0）。
 *
 * 为什么先做这个、且它与"换不换组件库"无关：
 * 实测 `src` 里有 **478 处硬编码颜色字面值**，而 CSS 变量引用只有 32 处（见
 * `docs/UI_UX_REDESIGN_BRIEF.md` §1.2）。在这种底子上一页一页改观感，
 * 换任何库都还是"每块界面各按各的颜色"。所以先把颜色/间距/字号/圆角/阴影/动效
 * 收成一份 TS 真源，运行时派生成 `--aeg-*` CSS 变量，组件库的主题对象也从这里派生。
 *
 * 三条硬规矩（都有测试钉着，见 `tokens.spec.ts`）：
 * 1. **间距一律 4px 基数**，字号/圆角/动效只许取自这几档，不许出现"顺手写个 13px"；
 * 2. **两主题的语义槽位必须同名同集合**——少一个键，切到深色就是规则里点名的"白底补丁"；
 * 3. **文字槽位对它所在表面必须过 WCAG AA**（正文 ≥4.5:1，大字号 ≥3:1），
 *    深浅两主题都算，算不过就不许合。数字由测试现算，不是这里手写的。
 *
 * 风险等级色**不在这里定义**：那是数据语义，真源在 `api/types.ts` 的 `RISK_COLORS`
 * （门禁第 9 条钉的就是"图例与图上必须同一份"）。这里只补"当它当文字用时"的两套可读变体。
 */

import { RISK_COLORS, type RiskLevel } from '@/api/types'

/** 品牌色阶（浅色主题用偏深的蓝，深色主题用偏亮的蓝——同一族，两个方向） */
export const brand = {
  /** 浅色主题主色：白字压上去 5.98:1（AA 过） */
  light: {
    base: '#2563EB',
    hover: '#1D4ED8',
    active: '#1E40AF',
    soft: 'rgba(37, 99, 235, 0.12)',
    line: 'rgba(37, 99, 235, 0.22)',
  },
  /** 深色主题主色：底是深蓝黑，同一段蓝要提一档才够对比 */
  dark: {
    base: '#3B82F6',
    hover: '#60A5FA',
    active: '#2563EB',
    soft: 'rgba(59, 130, 246, 0.20)',
    line: 'rgba(59, 130, 246, 0.34)',
  },
} as const

/** 语义槽位：两主题同名，值不同。少一个键就是换肤破绽。 */
export const colorSlots = {
  light: {
    page: '#F2F4F8',
    card: '#FFFFFF',
    elevated: '#FFFFFF',
    sunken: '#EEF1F6',
    alter: '#F7F9FC',
    border: '#E6E8EF',
    'border-subtle': '#EEF0F5',
    'border-strong': '#D4D8E3',
    text: '#0A0A0C',
    'text-secondary': '#5A6072',
    /**
     * 灰字这一档是**测出来的**：原来的 #6B7488 落在白卡上 4.69:1 勉强过，
     * 但落到 `alter`(#F7F9FC) 只有 4.45、落到页面底 #F2F4F8 只有 4.26 —— 都不到 AA 的 4.5。
     * 压深到 #626B7D 之后：card 5.36 / alter 5.08 / page 4.87，三处都过（`tokens.spec.ts` 钉住）。
     */
    'text-muted': '#626B7D',
    'text-inverse': '#FFFFFF',
    brand: brand.light.base,
    'brand-hover': brand.light.hover,
    'brand-active': brand.light.active,
    'brand-soft': brand.light.soft,
    'brand-line': brand.light.line,
    'status-success': '#15803D',
    'status-warning': '#B45309',
    'status-danger': '#B91C1C',
    'status-info': '#1D4ED8',
    'overlay-scrim': 'rgba(15, 23, 42, 0.45)',
    'editor-canvas': '#F6F7F9',
    'editor-header': '#FFFFFF',
    'editor-panel': '#FFFFFF',
    'editor-node': '#FFFFFF',
    'editor-node-border': '#E4E7EC',
    'editor-dot': '#D0D5DD',
    'editor-edge': '#98A2B3',
    'editor-note-bg': '#FEF7E6',
    'editor-note-border': '#F0D79B',
    'editor-cat-io': '#2563EB',
    'editor-cat-logic': '#7C3AED',
    'editor-cat-intelligence': '#0F766E',
    'editor-cat-human': '#B45309',
    'editor-accent-blue': '#2563EB',
    'editor-accent-violet': '#7C3AED',
    'editor-accent-teal': '#0F766E',
    'editor-accent-amber': '#B45309',
    'editor-accent-rose': '#BE123C',
    'editor-accent-slate': '#475569',
    'editor-state-running': '#0E7490',
    'editor-state-awaiting': '#7C3AED',
  },
  dark: {
    page: '#0B1220',
    card: '#111A2B',
    elevated: '#16223A',
    sunken: '#0E1728',
    alter: '#13203A',
    border: '#24344F',
    'border-subtle': '#1B2942',
    'border-strong': '#33475F',
    text: '#E6EDF7',
    'text-secondary': '#A7B4C9',
    'text-muted': '#8595AD',
    'text-inverse': '#0B1220',
    brand: brand.dark.base,
    'brand-hover': brand.dark.hover,
    'brand-active': brand.dark.active,
    'brand-soft': brand.dark.soft,
    'brand-line': brand.dark.line,
    'status-success': '#4ADE80',
    'status-warning': '#FBBF24',
    'status-danger': '#F87171',
    'status-info': '#60A5FA',
    'overlay-scrim': 'rgba(2, 6, 16, 0.62)',
    'editor-canvas': '#151A21',
    'editor-header': '#171D26',
    'editor-panel': '#1A202B',
    'editor-node': '#1D232D',
    'editor-node-border': '#344054',
    'editor-dot': '#3A4354',
    'editor-edge': '#5A6B82',
    'editor-note-bg': '#3A2E1A',
    'editor-note-border': '#6E5322',
    'editor-cat-io': '#60A5FA',
    'editor-cat-logic': '#A78BFA',
    'editor-cat-intelligence': '#2DD4BF',
    'editor-cat-human': '#FBBF24',
    'editor-accent-blue': '#60A5FA',
    'editor-accent-violet': '#A78BFA',
    'editor-accent-teal': '#2DD4BF',
    'editor-accent-amber': '#FBBF24',
    'editor-accent-rose': '#FB7185',
    'editor-accent-slate': '#94A3B8',
    'editor-state-running': '#22D3EE',
    'editor-state-awaiting': '#C4B5FD',
  },
} as const satisfies Record<'light' | 'dark', Record<string, string>>

export type ThemeMode = 'light' | 'dark'
export type ColorSlot = keyof (typeof colorSlots)['light']

/**
 * 风险等级色当**文字**用时的变体（图例、芯片字、指标数字）。
 * 底色的那一份仍取 `RISK_COLORS`；这里只解决"红/黄压在白底或深蓝黑底上读不读得清"。
 * 每档都要过 AA，数值由 `tokens.spec.ts` 现算现验。
 */
export const riskText: Record<ThemeMode, Record<RiskLevel, string>> = {
  light: { 1: '#B91C1C', 2: '#C2410C', 3: '#A16207', 4: '#1D4ED8', 5: '#525252' },
  dark: { 1: '#FCA5A5', 2: '#FDBA74', 3: '#FDE047', 4: '#93C5FD', 5: '#D4D4D8' },
}

/** 风险等级底色（图例块、地图填充）：深色主题下要压暗一档，否则整块在暗背景上"发光" */
export const riskFill: Record<ThemeMode, Record<RiskLevel, string>> = {
  light: { 1: RISK_COLORS[1], 2: RISK_COLORS[2], 3: RISK_COLORS[3], 4: RISK_COLORS[4], 5: RISK_COLORS[5] },
  dark: { 1: '#EF4444', 2: '#F97316', 3: '#EAB308', 4: '#3B82F6', 5: '#71717A' },
}

/**
 * 状态标签（芯片）一族：底色 / 描边 / 字色成套，两主题同一组语义键。
 *
 * 底色给的是**不透明值**：`theme.css` 原来写的是 `rgba(34,197,94,.12)` 这种半透雾，
 * 叠在白卡和叠在深蓝黑卡上会得到两个合成色，AA 就没法在令牌层一次算清
 * （阶段 0 那次灰字事故正是"只对某一块底成立"）。这里把合成结果落成实色，
 * `tokens.spec.ts` 才能对每一对文字/底色现算现验。
 */
export type TagTone = 'green' | 'orange' | 'blue' | 'red'
export const tagTone: Record<ThemeMode, Record<TagTone, { bg: string; line: string; text: string }>> = {
  light: {
    green: { bg: '#EAF6EE', line: '#BFE3C9', text: '#166534' },
    orange: { bg: '#FDF1E2', line: '#F3D3A5', text: '#8A4005' },
    blue: { bg: '#E9F0FE', line: '#C3D6F8', text: '#1E40AF' },
    red: { bg: '#FDECEC', line: '#F6C4C4', text: '#B91C1C' },
  },
  dark: {
    green: { bg: '#12301F', line: '#2F6A48', text: '#6EE7B7' },
    orange: { bg: '#33240D', line: '#6E4E1B', text: '#FBBF24' },
    blue: { bg: '#14243F', line: '#2C4C7E', text: '#93C5FD' },
    red: { bg: '#3B191D', line: '#7A2C33', text: '#FCA5A5' },
  },
}

export function riskColor(level: RiskLevel | null, mode: ThemeMode = 'light'): string {
  return level === null ? colorSlots[mode]['text-muted'] : riskFill[mode][level]
}

export function riskTextColor(level: RiskLevel | null, mode: ThemeMode = 'light'): string {
  return level === null ? colorSlots[mode]['text-muted'] : riskText[mode][level]
}

/** 间距：4px 基数，只许这八档（DoD 里"间距符合 4px 网格"就是这条） */
export const space = { s1: 4, s2: 8, s3: 12, s4: 16, s5: 20, s6: 24, s8: 32, s10: 40, s12: 48 } as const

/** 字号阶（11 是给等宽徽标/时间戳留的最小档，再小现场读不清） */
export const font = {
  size: { xs: 11, sm: 12, base: 14, md: 16, lg: 20, xl: 24, '2xl': 32 },
  weight: { regular: 400, medium: 500, semibold: 600, bold: 700 },
  lineHeight: { tight: 1.2, snug: 1.4, normal: 1.5, relaxed: 1.6 },
} as const

/** 圆角：三档 + 胶囊。取 6/10/16 是为了**与今天的实测值一致**（旧 ConfigProvider 就是 10 与 16），
 *  半径不参与 4px 网格（那是间距的规矩），先不动它以免顺手改出视觉回归。 */
export const radius = { sm: 6, md: 10, lg: 16, pill: 999 } as const

export const shadow: Record<ThemeMode, Record<'sm' | 'md' | 'lg', string>> = {
  light: {
    sm: '0 1px 2px rgba(15, 23, 42, 0.06)',
    md: '0 1px 2px rgba(15, 23, 42, 0.06), 0 4px 12px rgba(15, 23, 42, 0.08)',
    lg: '0 8px 28px rgba(15, 23, 42, 0.12)',
  },
  dark: {
    sm: '0 1px 2px rgba(0, 0, 0, 0.40)',
    md: '0 1px 2px rgba(0, 0, 0, 0.40), 0 6px 16px rgba(0, 0, 0, 0.36)',
    lg: '0 12px 32px rgba(0, 0, 0, 0.48)',
  },
}

/** 动效时长：与 theme.css 的 `--motion-*` 同一份（测试会比对，防两处漂移） */
export const motion = { fast: '0.1s', base: '0.15s', slow: '0.2s', draw: '0.6s' } as const

export const z = { sticky: 10, header: 100, drawer: 200, modal: 300, toast: 400 } as const

/** 顶栏定高：提示条 top 与"浮层不压顶栏"那条判据都依赖它，收成一处 */
export const layout = {
  headerHeight: 68,
  sidebarWidth: 232,
  sidebarCollapsed: 64,
  contentMaxWidth: 1720,
  editorHeaderHeight: 56,
  editorPanelWidth: 400,
  nodeWidth: 240,
} as const

/** 字体栈（离线自托管，不外链 CDN） */
export const fontFamily = {
  sans: "'Space Grotesk', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', 'Noto Sans SC', sans-serif",
  mono: "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace",
} as const
