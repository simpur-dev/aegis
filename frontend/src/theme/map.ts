/**
 * Cesium 场景的令牌派生层（阶段 1 后半）。
 *
 * 与 `chart.ts` 同一条理由：WebGL 画的是位图，**读不到 CSS 变量**，所以"所有表面随主题换肤"
 * 在地图这一面上只能把 `colorSlots[mode]` 的同一批值再喂一份给场景。这一层
 * **一个字的色值都不许自己写**（`uiTheme.spec.ts` 第 12 条把它列进派生层零 hex 名单），
 * 改了令牌画面必须跟着改，否则又是一张皮。
 *
 * 覆盖哪些面（改造前的取证：`viewer.ts` 里钉死 `GLOBE_BASE_COLOR = '#101923'` 与
 * `Color.WHITE/BLACK` 四类图元色，深色浅色两档画出来是同一张图）：
 * - 无影像时的地球底色与画面外太空底色；
 * - 文字标签的字色、描边（光晕）与底色；
 * - 站点/触达/聚合簇的描边色；
 * - 风险填充色：图元带的是浅色档（`riskFill.light`），深色档要压暗一档才不在暗底上"发光"，
 *   这里按 `riskFill.light → riskFill.dark` 做逐档映射，映射不到的（等级未知灰、通道红蓝）原样透出。
 *
 * 太阳/月亮/天空大气**两档都关掉**：它们是不表达任何状态的装饰光，
 * 而且深色档下那条蓝色地平线辉光是画布里唯一不肯翻面的亮面。
 */

import { colorSlots, riskFill, type ThemeMode } from './tokens'
import type { RiskLevel } from '@/api/types'

export interface MapPalette {
  readonly mode: ThemeMode
  /** 没有影像瓦片时的地球底色：浅色档给暗一版的沉底，深色档给近黑的深蓝，两档都不刺眼。 */
  readonly globeBase: string
  /** 地球之外那一片（太空）的底色：让画面边缘与页面底色同族，不留一块与主题无关的黑。 */
  readonly spaceBackdrop: string
  /** 标签字色：浅档近黑、深档近白，对 `labelBackground` 实算过 AA（见 map.spec.ts）。 */
  readonly labelText: string
  /** 标签描边（光晕）：与字色反向，压在影像或地形上都读得出。 */
  readonly labelHalo: string
  /** 标签底板色（配合 `labelBackgroundAlpha` 使用）。 */
  readonly labelBackground: string
  /** 图元描边（点与聚合环）：与字色同档，负责把风险色块从底图里"抠"出来。 */
  readonly markerOutline: string
  /** 深空皮肤：拉远到看得见地球弧线时用的球面底色与太空底（两主题同值）。 */
  readonly spaceGlobe: string
  readonly deepSpace: string
  readonly labelBackgroundAlpha: number
  /** 风险填充档：把上游给的任一档 hex 归一到当前主题的对应档；认不出的原样返回。 */
  readonly fillFor: (cssColor: string) => string
}

/**
 * 风险档的色值对照表：只在深色档做替换，浅色档一律原样透出。
 *
 * 键与查询都归成大写：令牌里写的是 `#DC2626` 这种大写，而 Cesium 侧一路可能吐小写
 * （`Color.toCssColorString()` 给的就是小写），不归一的话"映射不到"会静默退回浅色档那一支，
 * 深色画面上就留下一颗不该存在的亮色标记。
 */
function riskMapping(mode: ThemeMode): Record<string, string> {
  if (mode === 'light') return {}
  const map: Record<string, string> = {}
  for (const [level, lightCss] of Object.entries(riskFill.light)) {
    map[lightCss.toUpperCase()] = riskFill.dark[Number(level) as RiskLevel]
  }
  return map
}

export function mapPalette(mode: ThemeMode): MapPalette {
  const c = colorSlots[mode]
  const mapping = riskMapping(mode)
  return {
    mode,
    globeBase: c.sunken,
    spaceBackdrop: c.page,
    labelText: c.text,
    labelHalo: c.card,
    labelBackground: c.elevated,
    markerOutline: c.text,
    spaceGlobe: c['space-globe'],
    deepSpace: c['deep-space'],
    labelBackgroundAlpha: 0.82,
    fillFor: (cssColor) => mapping[cssColor.toUpperCase()] ?? cssColor,
  }
}

/**
 * 高德 JS API 的底图样式名（`AmapMap.vue` 用）。
 *
 * 高德把样式做成它自家渲染器里的**字符串档位**，CSS 变量到达不了画布——
 * 与 Cesium/ECharts 同一条纪律：档位映射住在本派生层，组件不许自己挑样式名。
 * 浅色用官方默认 normal；深色用灰度档 grey（比纯黑档 dark 更贴本平台深蓝黑的卡面色）。
 */
export function amapStyleName(mode: ThemeMode): string {
  return mode === 'dark' ? 'amap://styles/grey' : 'amap://styles/normal'
}
