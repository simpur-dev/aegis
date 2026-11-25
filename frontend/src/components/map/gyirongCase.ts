import type { BBox, LngLat } from './entities'

export interface HistoricalSite {
  id: string
  label: string
  action: string
  lonlat: LngLat
  mainlandChina: boolean
  note: string
}

export const GYIRONG_CASE = {
  date: '2026-08-26',
  bounds: { west: 85.292, south: 28.193, east: 85.577, north: 28.355 } satisfies BBox,
  sites: [
    {
      id: 'history-gyirong-port',
      label: '吉隆口岸',
      action: '定位口岸',
      lonlat: { lon: 85.3778, lat: 28.2797 },
      mainlandChina: true,
      note: '口岸受灾区域的参考位置，海拔约 1,800 米。',
    },
    {
      id: 'history-gyirong-upstream',
      label: '上游事件定位',
      action: '定位上游',
      lonlat: { lon: 85.515, lat: 28.271 },
      mainlandChina: false,
      note: '此点引用 USGS 地震事件定位，供查找上游区域参考。冰岩崩启动区以遥感解译为准。',
    },
  ] satisfies readonly HistoricalSite[],
  sources: [
    { label: '地调局简报', url: 'https://www.cgs.gov.cn/ywdt/ddyw/202608/t20260828_867531.html' },
    { label: '灾情通报', url: 'https://www.xinhuanet.com/politics/20260904/12f2ff2ed0964c848e3cf3e9ca36212a/c.html' },
    { label: '中科院遥感解译', url: 'https://apm.cas.cn/kydt/202609/t20260907_8278940.html' },
    { label: '事件坐标', url: 'https://ngf.earthscope.org/geophysical-event/us7000tbwb-nepal-glacial-collapse-and-debris-flow' },
  ],
}
