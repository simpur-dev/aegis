/**
 * 地形通道：自托管 quantized-mesh（`/terrain`）与**椭球面兜底**之间的选择。
 *
 * 拆成两层的理由：
 * - `chooseTerrainMode()` 是纯决策（给定探针事实 → 模式 + 理由），可单测、可在没烘焙资产时先跑通；
 * - `createTerrainSetup()` 才是装配：接收注入的 Cesium 命名空间（`viewer.ts` 里动态 import 的那一份），
 *   本模块自身**不**产生 cesium 运行时导入（只有 `import type`，编译期即被抹掉）。
 *
 * 为什么一定要兜底：高原现场常见"地形瓦片还没烘焙/带回营地才补"，
 * 此时页面必须仍然出图（椭球面 + 影像 + 矢量要素），而不是 `fromUrl` 抛错整页崩。
 * 椭球面的代价只有一点：贴地要素的钳地高度按海拔 0 计算，因此 `entityOptions` 侧仍会
 * 用台账里的 `elevation_m` 给站点设绝对高度（见 viewer.ts 的 heightReference 选择）。
 */

import type { CesiumTerrainProvider, EllipsoidTerrainProvider } from 'cesium'

import { isLocalAssetUrl } from './offline'

export type TerrainMode = 'quantized-mesh' | 'ellipsoid'

/** quantized-mesh 服务器必须暴露的元数据入口；探不到它就不要尝试。 */
export const TERRAIN_METADATA_SUFFIX = 'layer.json'

export const DEFAULT_TERRAIN_URL = '/terrain'

export interface TerrainProbeInput {
  /** 配置的地形服务地址（同源路径）；null/空 表示没配。 */
  configuredUrl: string | null
  /** `layer.json` 探针是否拿得到（由 offline.probeSource 给出）。 */
  layerJsonReachable: boolean
  browserOnline: boolean
  origin?: string
}

export interface TerrainChoice {
  mode: TerrainMode
  reason: string
  url: string | null
}

/** 去掉尾部斜杠，避免拼出 `/terrain//layer.json`。 */
export function normalizeTerrainUrl(url: string): string {
  return url.replace(/\/+$/, '')
}

export function terrainProbeUrl(url: string): string {
  return `${normalizeTerrainUrl(url)}/${TERRAIN_METADATA_SUFFIX}`
}

/**
 * 兜底决策（顺序即优先级，全部命中才用 quantized-mesh）：
 * 1. 没配地址 → 椭球面；
 * 2. 地址指向外部主机 → 椭球面（本项目禁用任何第三方地形服务，包括官方的世界地形）；
 * 3. 浏览器断网且探针不通 → 椭球面；
 * 4. `layer.json` 探不到 → 椭球面（瓦片没烘焙）。
 */
export function chooseTerrainMode(input: TerrainProbeInput): TerrainChoice {
  const url = input.configuredUrl ? normalizeTerrainUrl(input.configuredUrl) : ''
  if (!url) return { mode: 'ellipsoid', reason: '未配置自托管地形服务，使用椭球面', url: null }
  if (!isLocalAssetUrl(url, input.origin)) {
    return { mode: 'ellipsoid', reason: `地形地址 ${url} 非同源，已按无外链约束拒绝`, url: null }
  }
  if (!input.layerJsonReachable && !input.browserOnline) {
    return { mode: 'ellipsoid', reason: '断网且本地地形未烘焙，使用椭球面', url }
  }
  if (!input.layerJsonReachable) {
    return { mode: 'ellipsoid', reason: `未取到 ${terrainProbeUrl(url)}，地形瓦片尚未烘焙，使用椭球面`, url }
  }
  return { mode: 'quantized-mesh', reason: `自托管 quantized-mesh：${url}`, url }
}

/** 只声明用到的静态方法：这样 `viewer.ts` 注入整个 cesium 命名空间也满足形状。 */
export interface TerrainCesium {
  CesiumTerrainProvider: typeof CesiumTerrainProvider
  EllipsoidTerrainProvider: typeof EllipsoidTerrainProvider
}

export interface TerrainSetup {
  mode: TerrainMode
  provider: InstanceType<TerrainCesium['CesiumTerrainProvider']> | InstanceType<TerrainCesium['EllipsoidTerrainProvider']>
  message: string
}

/**
 * 装配地形 provider。即使 `chooseTerrainMode` 判为 quantized-mesh，
 * `fromUrl` 仍可能失败（layer.json 在但瓦片编码不合、Cesium 版本口径变更等），
 * 这里兜底再退椭球面 —— 兜底链必须有两级，因为探针只证明"文件在"，不证明"能解析"。
 */
export async function createTerrainSetup(
  cesium: TerrainCesium,
  choice: TerrainChoice,
): Promise<TerrainSetup> {
  if (choice.mode === 'quantized-mesh' && choice.url) {
    try {
      const provider = await cesium.CesiumTerrainProvider.fromUrl(choice.url, {
        requestVertexNormals: true,
        requestWaterMask: false,
      })
      return { mode: 'quantized-mesh', provider, message: choice.reason }
    } catch (error) {
      return {
        mode: 'ellipsoid',
        provider: new cesium.EllipsoidTerrainProvider(),
        message: `quantized-mesh 初始化失败（${describeError(error)}），已回退椭球面`,
      }
    }
  }
  return {
    mode: 'ellipsoid',
    provider: new cesium.EllipsoidTerrainProvider(),
    message: choice.reason,
  }
}

export function describeError(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error)
}
