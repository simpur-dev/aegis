/**
 * 时间的显示口径（零依赖，可离线单测）。
 *
 * 线上与存储一律 UTC 毫秒 ISO（后端唯一真源 `domain/messages.py: now_iso()`，带 `Z`），
 * 这是对的：跨组件比对、落库、契约校验都不该掺时区。但**界面不能就这么给人看**——
 * 值班指挥员读的是墙上时钟，`2026-10-03T16:45:13.840Z` 对他意味着"明天凌晨 00:45"，
 * 差八小时；而应急平台上一条预警差八小时不是排版问题。
 *
 * 所以这里固定换算到东八区（全国同一时区，西藏亦按北京时间执钟），并**不跟随浏览器时区**：
 * 跟着浏览器走会让同一条预警在不同机器上显示成两个时刻，而"这条预警什么时候发的"
 * 必须只有一个答案。表头/轴标签同时带上 `UTC+8`，不让人靠猜。
 */

export const OPERATING_OFFSET_HOURS = 8

const ISO_SHAPE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?\d*Z$/

const pad = (value: number, width = 2): string => String(value).padStart(width, '0')

/** 解析后端那个形状的 UTC ISO。解析不了就返回 null，由调用方决定显示什么——不猜。 */
function parseUtc(iso: string): Date | null {
  if (!ISO_SHAPE.test(iso)) return null
  const moment = new Date(iso)
  return Number.isNaN(moment.getTime()) ? null : moment
}

/** `2026-10-03T16:45:13.840Z` → `2026-10-04 00:45:13`（东八区，含跨日）。 */
export function formatOperatingTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const moment = parseUtc(iso)
  if (!moment) return iso
  const shifted = new Date(moment.getTime() + OPERATING_OFFSET_HOURS * 3_600_000)
  return (
    `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())} ` +
    `${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}:${pad(shifted.getUTCSeconds())}`
  )
}

/** 图表轴用的短形态：只给 `00:45:13`。轴上放不下完整日期，但放不下不等于可以不标时区。 */
export function operatingClock(iso: string | null | undefined): string {
  const full = formatOperatingTime(iso)
  const space = full.indexOf(' ')
  return space > 0 ? full.slice(space + 1) : full
}
