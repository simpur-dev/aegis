/**
 * 底图测试共用的 Cesium 几何替身（按**度**造假矩形）。
 *
 * 为什么值得共用：`PmtilesImageryProvider` 现在依赖一条 Cesium 前提——「imagery TilingScheme 的
 * rectangle 总是完全包含 ImageryProvider 的 rectangle」（ImageryLayer.js 源码注释原文），
 * 越界会让 `positionToTileXY` 返回 undefined 并打死渲染循环。两个 spec 都要仿这条前提，
 * 各自手写就会出现"两份假交集算出不同结果"，那比没测更糟。
 *
 * 真 Cesium 存的是弧度；这里用度是因为被测代码只做「矩形取交集」，用度写断言一眼能看出错在哪。
 * 弧/度换算由 Cesium 自己负责，`Rectangle.intersection` 的经纬处理与之无关。
 */

/** Web Mercator 的纬度上界（度）：切片方案把 ±(椭球半长轴·π) 米反投影后得到的就是这个值。 */
export const WEB_MERCATOR_MAX_LATITUDE_DEG = 85.05112877980659

export interface DegreeRect {
  west: number
  south: number
  east: number
  north: number
}

/** 与 `Rectangle.intersection` 同口径：边界取内侧的 max/min，退化成线或空就返回 undefined。 */
export function intersectRectangles(a: DegreeRect, b: DegreeRect): DegreeRect | undefined {
  const clipped = {
    west: Math.max(a.west, b.west),
    south: Math.max(a.south, b.south),
    east: Math.min(a.east, b.east),
    north: Math.min(a.north, b.north),
  }
  return clipped.south >= clipped.north || clipped.west >= clipped.east ? undefined : clipped
}

export function webMercatorSchemeRectangle(): DegreeRect {
  return { west: -180, south: -WEB_MERCATOR_MAX_LATITUDE_DEG, east: 180, north: WEB_MERCATOR_MAX_LATITUDE_DEG }
}

export const fakeRectangle = {
  fromDegrees: (west: number, south: number, east: number, north: number): DegreeRect => ({ west, south, east, north }),
  intersection: intersectRectangles,
}

export class FakeWebMercatorTilingScheme {
  readonly rectangle: DegreeRect = webMercatorSchemeRectangle()
}

/** 是否落在 Web Mercator 切片方案内——provider 矩形越界就是浏览器里那次崩溃的直接原因。 */
export function insideWebMercator(rect: DegreeRect): boolean {
  const scheme = webMercatorSchemeRectangle()
  return (
    rect.west >= scheme.west &&
    rect.east <= scheme.east &&
    rect.south >= scheme.south &&
    rect.north <= scheme.north
  )
}
