/**
 * WGS84 → GCJ-02 纠偏（接高德等国内在线底图时**必须**用得上）。
 *
 * 为什么不能省：后端与设备给的是 WGS84（GPS 原始经纬度），而高德/天地图在线瓦走 GCJ-02
 * （国测局加密坐标）。两者在西藏一带差 **约 300–600 米**——画面看着一切正常，落点却整体偏移，
 * 这正是应急产品里最不该有的那种错：图上位置与数据里的经纬度不是一回事，而没人看得出来。
 *
 * 算法是公开的通用实现（Krasovsky 1940 椭球 + 正弦扰动），但**参数与判据以实测为准**：
 * `gcj02.spec.ts` 钉住"境外不偏移、境内必偏移、偏移量在百米量级、且不可幂等重复施加"。
 */
import type { LngLat } from './entities'

const A = 6378245.0
const EE = 0.00669342162296594323

function outOfChina(lng: number, lat: number): boolean {
  return !(72.004 <= lng && lng <= 137.8347 && 0.8293 <= lat && lat <= 55.8271)
}

function transformLat(x: number, y: number): number {
  let ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x))
  ret += ((20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0) / 3.0
  ret += ((20.0 * Math.sin(y * Math.PI) + 40.0 * Math.sin((y / 3.0) * Math.PI)) * 2.0) / 3.0
  ret += ((160.0 * Math.sin((y / 12.0) * Math.PI) + 320 * Math.sin((y * Math.PI) / 30.0)) * 2.0) / 3.0
  return ret
}

function transformLng(x: number, y: number): number {
  let ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x))
  ret += ((20.0 * Math.sin(6.0 * x * Math.PI) + 20.0 * Math.sin(2.0 * x * Math.PI)) * 2.0) / 3.0
  ret += ((20.0 * Math.sin(x * Math.PI) + 40.0 * Math.sin((x / 3.0) * Math.PI)) * 2.0) / 3.0
  ret += ((150.0 * Math.sin((x / 12.0) * Math.PI) + 300.0 * Math.sin((x / 30.0) * Math.PI)) * 2.0) / 3.0
  return ret
}

/** WGS84 → GCJ-02。境外点原样返回（该体系只对境内做扰动）。 */
export function wgs84ToGcj02(point: LngLat): LngLat {
  if (outOfChina(point.lon, point.lat)) return { ...point }
  let dLat = transformLat(point.lon - 105.0, point.lat - 35.0)
  let dLng = transformLng(point.lon - 105.0, point.lat - 35.0)
  const radLat = (point.lat / 180.0) * Math.PI
  let magic = Math.sin(radLat)
  magic = 1 - EE * magic * magic
  const sqrtMagic = Math.sqrt(magic)
  dLat = (dLat * 180.0) / (((A * (1 - EE)) / (magic * sqrtMagic)) * Math.PI)
  dLng = (dLng * 180.0) / ((A / sqrtMagic) * Math.cos(radLat) * Math.PI)
  return { ...point, lon: point.lon + dLng, lat: point.lat + dLat }
}

/**
 * 两点间的近似地面距离（米），只用于断言"偏移在百米量级"，不是测绘精度。
 * 用等角圆柱近似即可：这个量级上椭球修正不影响判据。
 */
export function approxMeters(a: LngLat, b: LngLat): number {
  const latRad = (((a.lat + b.lat) / 2) * Math.PI) / 180
  const dLat = (b.lat - a.lat) * 111_320
  const dLng = (b.lon - a.lon) * 111_320 * Math.cos(latRad)
  return Math.hypot(dLat, dLng)
}
