/**
 * 静态地形示意底图的场景锚点（归一化 0–100，左上角为原点）。
 *
 * 底图是艺术化的三维切片，**没有经纬网**，所以这里不是地理投影；
 * 每个位置都需要人工对照底图校准。没有校准记录的区域不要填——
 * 列表会如实标「未配置场景位置」，绝不按经纬度或随机数在图上放点。
 *
 * 下面三个是演示数据里已有的区划，先给“示意点”让列表↔标记联动可验收；
 * 接入真实区划前必须逐个重校。
 */
import type { RiskLevel } from '@/api/types'

export interface SceneAnchor {
  x: number
  y: number
}

/** 静态地形上的一个风险落点（与底部风险一览共用同一份业务字段） */
export interface SceneCell {
  id: string
  region: string
  hazard: string
  level: RiskLevel
  /** 归一化场景坐标（0–100）；缺省表示尚未校准，不上图 */
  anchor?: SceneAnchor
}

const DEMO_ANCHORS: Record<string, SceneAnchor> = {
  '540121': { x: 31, y: 46 },
  '540221': { x: 57, y: 34 },
  '540321': { x: 68, y: 52 },
}

export function anchorForRegion(region: string): SceneAnchor | undefined {
  return DEMO_ANCHORS[region]
}
