import * as echarts from 'echarts/core'
import type { ECharts, EChartsCoreOption } from 'echarts/core'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TitleComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

// 按需注册：只引入折线/柱状与必要组件，避免把整个 echarts 打进首屏
echarts.use([BarChart, LineChart, GridComponent, LegendComponent, TitleComponent, TooltipComponent, CanvasRenderer])

/** 视图层构造的图表配置；类型宽松换取声明式书写自由，运行时由 echarts 校验。 */
export type ChartOption = Record<string, unknown>
export type Chart = ECharts
export type CoreOption = EChartsCoreOption

export { echarts }
