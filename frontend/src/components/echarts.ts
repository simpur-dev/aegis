import * as echarts from 'echarts/core'
import type { ECharts, EChartsCoreOption } from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

import { chartTheme, chartThemeName } from '@/theme/chart'

// 按需注册：只引入折线/柱状与必要组件，避免把整个 echarts 打进首屏
echarts.use([BarChart, LineChart, GridComponent, LegendComponent, TitleComponent, TooltipComponent, CanvasRenderer])

/**
 * 两套主题在模块加载时就注册好：`EChart.vue` 只按名字取，
 * 谁先初始化都不依赖顺序（注册晚于 init 会静默退回内置主题——那正是这次要修的病）。
 */
echarts.registerTheme(chartThemeName.light, chartTheme('light'))
echarts.registerTheme(chartThemeName.dark, chartTheme('dark'))

/** 视图层构造的图表配置；类型宽松换取声明式书写自由，运行时由 echarts 校验。 */
export type ChartOption = Record<string, unknown>
export type Chart = ECharts
export type CoreOption = EChartsCoreOption

export { echarts }
