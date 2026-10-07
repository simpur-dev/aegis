/**
 * ECharts 主题派生层（阶段 1 后半）。
 *
 * 为什么还要再派生一份：画布是位图，**读不到 CSS 变量**——`var(--aeg-color-text)` 对
 * `canvas.getContext('2d')` 没有意义。所以"所有表面随主题换肤"在 ECharts 这一面上只有一条路：
 * 把 `colorSlots[mode]` 的同一批值交给 `echarts.registerTheme`。这一层与 `antd.ts` 同规则，
 * **一个字的色值都不许自己写**（`uiTheme.spec.ts` 第 12 条把它列进派生层零 hex 名单），
 * 换了令牌画布必须跟着换，否则又是一张皮。
 *
 * 取证（改造前，`E:\tmp\chart-probe.mjs` 在 4175 构建产物上量画布像素）：深色主题下
 * `/monitor` 的 33151 个成笔像素里 30220 个对比只落在 3:1–4.5:1、494 个不足 3:1，
 * `/metrics` 23090/52672 同档——轴线/轴标/图例/标题全是 ECharts 自带浅色调色板，
 * 折线甚至画成了它默认的草绿 #91cc75，而不是平台主色。
 *
 * 档位的取法（数字是 `tokens.spec.ts` 与本文件改动前各自算出来的，不是手写的）：
 * - 文字（轴标、图例、标题）走 `text-muted` / `text-secondary` / `text`，对 `card` 底
 *   浅 5.36 / 6.27 / 19.78，深 5.72 / 8.30 / 14.77，tooltip 的字在 `elevated` 上深 13.46；
 * - 轴线走 `border-strong`、网格线走 `border-subtle`：网格线是"辅助读数"的装饰，
 *   故意不过 3:1，压到 AA 会变成比数据还显眼的第二主角；
 * - series 调色板只取语义槽位（主色 + 三档状态色 + 次级灰），不引入新色相。
 */

import { colorSlots, font, fontFamily, radius, space, type ThemeMode } from './tokens'

/** 注册进 echarts 的两个主题名；`EChart.vue` 按当前 resolved 模式选一个。 */
export const chartThemeName: Record<ThemeMode, string> = {
  light: 'aegis-light',
  dark: 'aegis-dark',
}

const axis = (mode: ThemeMode) => {
  const c = colorSlots[mode]
  return {
    axisLine: { lineStyle: { color: c['border-strong'] } },
    axisTick: { lineStyle: { color: c['border-strong'] } },
    axisLabel: { color: c['text-muted'], fontSize: font.size.sm },
    splitLine: { lineStyle: { color: c['border-subtle'] } },
    splitArea: { show: false },
  }
}

export function chartTheme(mode: ThemeMode): Record<string, unknown> {
  const c = colorSlots[mode]
  return {
    color: [c.brand, c['status-warning'], c['status-success'], c['status-danger'], c['brand-hover'], c['text-secondary']],
    /** 画布保持透明，让卡片的 `card` 底透出来：填一层实色就成了"第二块底"，主题翻转时最先漏馅。 */
    backgroundColor: 'transparent',
    textStyle: { color: c['text-secondary'], fontFamily: fontFamily.sans, fontSize: font.size.sm },
    title: {
      textStyle: { color: c.text, fontSize: font.size.base, fontWeight: font.weight.semibold },
      subtextStyle: { color: c['text-muted'], fontSize: font.size.sm },
    },
    legend: { textStyle: { color: c['text-secondary'], fontSize: font.size.sm }, inactiveColor: c['border-strong'], itemGap: space.s3 },
    tooltip: {
      backgroundColor: c.elevated,
      borderColor: c.border,
      borderRadius: radius.md,
      padding: [space.s2, space.s3],
      textStyle: { color: c.text, fontSize: font.size.sm },
      axisPointer: {
        lineStyle: { color: c['border-strong'] },
        crossStyle: { color: c['border-strong'] },
        label: { backgroundColor: c['brand-active'], color: c['text-inverse'] },
      },
    },
    categoryAxis: axis(mode),
    valueAxis: axis(mode),
    timeAxis: axis(mode),
    logAxis: axis(mode),
    grid: { borderColor: c['border-strong'] },
    line: { lineStyle: { width: 2 }, symbolSize: 6 },
    bar: { barMaxWidth: space.s8 },
  }
}
