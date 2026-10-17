/**
 * 底图档状态机（'amap' | 'offline'）。
 *
 * 历史与现状（2026-10-17）：本文件原来住着一条**无 key 的高德内部瓦片端点**
 * （`webrd0X.is.autonavi.com/appmaptile`，Cesium UrlTemplate 直连）——它不要 key、
 * 不走授权与署名要求，只能当"显式点选的本地试验档"。官方 JS API 接入（`amap.ts`，
 * key 走 `VITE_AMAP_KEY`）落地后，这条端点已**退役**：它不再有任何显示路径，
 * 主机名由 `guard.spec.ts` 全库禁入，不许回来。
 *
 * 现在两档的分工：
 * - 'offline'（默认）：Cesium 自托管降级链（basemap.ts/offline.ts/terrain.ts），只连同源资产；
 * - 'amap'：整块换成高德 JS API 渲染面（`AmapMap.vue`），要素坐标按 WGS84→GCJ-02 纠偏。
 *   场景层（viewer.ts）不感知这个档——它永远是离线链，换档是页面级"换组件"，不是换影像层。
 *
 * 默认档必须是 'offline'：交付形态不依赖任何外部主机；'amap' 依赖官方服务与配额，
 * 必须显式选过才生效并记忆。这条由 `external.spec.ts` 与 guard 判据一起钉住。
 */

export type BasemapMode = 'amap' | 'offline'

export const BASEMAP_MODE_KEY = 'aegis.basemap'

export function readBasemapMode(storage: Pick<Storage, 'getItem'> | null): BasemapMode {
  try {
    const raw = storage?.getItem(BASEMAP_MODE_KEY)
    return raw === 'amap' ? 'amap' : 'offline'
  } catch {
    return 'offline'
  }
}

export function writeBasemapMode(storage: Pick<Storage, 'setItem'> | null, mode: BasemapMode): void {
  try {
    storage?.setItem(BASEMAP_MODE_KEY, mode)
  } catch {
    // 隐私模式下 localStorage 会抛：底图切换当场生效即可，记不住不白屏
  }
}
