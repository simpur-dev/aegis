// @vitest-environment node
/**
 * 门禁：把烘焙出来的 quantized-mesh 交给**浏览器用的同一份 Cesium 模块**逐瓦解析。
 *
 * 为什么必须有这一条：`terrain.spec.ts` 喂的是假 Cesium 命名空间（证决策），
 * `offline-assets.spec.ts` 读的是瓦片头几个字段（证字节在位）。两者都证不了
 * "1.145 的解析器接受这张瓦，并且解出来的几何属于它自己那个矩形"。
 * 上一轮渲染崩溃就是这一层缺位造成的：影像链路的替身全绿，真 Cesium 在浏览器里直接
 * 把渲染循环打死（`undefined.x`）。地形这条腿不能再靠"看起来对"过关。
 *
 * 环境用 node 而不是 jsdom：这里不需要 DOM，需要的是真 `fetch`/Range 语义；
 * 并且 jsdom 造的 AbortSignal 不是 undici 认的那个实例（探针会在传输层死掉，
 * 于是"取不到"被读成"资产不在"）。`createMesh` 在无头里不可用（要 `document`），
 * 所以 GPU 侧的断言交给 Playwright 视觉烟雾门禁，这一条只管"解析与语义"。
 */

import { mkdtemp, copyFile, mkdir, readdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterAll, beforeAll, describe, expect, it } from 'vitest'

import * as Cesium from 'cesium'

import { publicAssetDir, startLocalAssetServer, type LocalAssetServer } from '@/testing/localAssetServer'
import { cesiumSourceFlat } from '@/testing/cesiumSource'
import { decodeTerrainTile, quantizedMeshFieldContract } from '@/testing/cesiumTerrainDecode'
import { readRepoFile } from '@/testing/repoSource'

/** 青藏高原一带：合成高程面的峰就在这里，用它来判"瓦的内容属于它自己的矩形"。 */
const PLATEAU = Cesium.Cartographic.fromDegrees(88.0, 31.0)

interface BakedTile {
  readonly level: number
  readonly x: number
  readonly y: number
}

function parseBakedPath(pathname: string): BakedTile | null {
  const match = /^(\d+)\/(\d+)\/(\d+)\.terrain$/.exec(pathname)
  if (!match) return null
  return { level: Number(match[1]), x: Number(match[2]), y: Number(match[3]) }
}

async function listBakedTiles(terrainDir: string): Promise<BakedTile[]> {
  const found: BakedTile[] = []
  for (const levelDir of await readdir(terrainDir)) {
    if (!/^\d+$/.test(levelDir)) continue
    for (const xDir of await readdir(join(terrainDir, levelDir))) {
      if (!/^\d+$/.test(xDir)) continue
      for (const file of await readdir(join(terrainDir, levelDir, xDir))) {
        const tile = parseBakedPath(`${levelDir}/${xDir}/${file}`)
        if (tile) found.push(tile)
      }
    }
  }
  return found.sort((a, b) => a.level - b.level || a.x - b.x || a.y - b.y)
}

/** 生成器里那句出处声明的唯一真源；抄进测试就只会证明"两份抄写一致"。 */
function generatorProvenance(): string {
  const source = readRepoFile('scripts', 'build_offline_tiles.py')
  const match = /^PROVENANCE = "(.+)"$/m.exec(source)
  if (!match) throw new Error('没抓到 PROVENANCE：build_offline_tiles.py 改写后这条门禁要跟着改（不是把断言删掉）')
  return match[1]
}

const PUBLIC_DIR = publicAssetDir()
const PROVENANCE = generatorProvenance()

let baked: BakedTile[] = []
let maxZoom = 0
/** 默认档：缺件回 404。用来跑"一切正常"的主干断言。 */
let strict: LocalAssetServer
/** provider 主干用例共享，避免为每张瓦重取一次 layer.json。 */
let provider: InstanceType<typeof Cesium.CesiumTerrainProvider>

beforeAll(async () => {
  strict = await startLocalAssetServer({ missing: 'not-found' })
  baked = await listBakedTiles(join(PUBLIC_DIR, 'terrain'))
  const layer = (await (await fetch(`${strict.origin}/terrain/layer.json`)).json()) as { maxzoom?: number }
  maxZoom = Number(layer.maxzoom)
  provider = await Cesium.CesiumTerrainProvider.fromUrl(`${strict.origin}/terrain`, { requestVertexNormals: true })
}, 60_000)

afterAll(async () => {
  await strict.close()
})

describe('真 Cesium 接不接受我们烘的地形', () => {
  it('layer.json 被吃下：切片方案是 Geographic，且请求路径就是我们声明的 {z}/{x}/{y}（不翻 y）', async () => {
    expect(provider.tilingScheme.constructor.name).toBe('GeographicTilingScheme')

    const target = baked.find((tile) => tile.level === maxZoom)!
    strict.requests.length = 0
    await provider.requestTileGeometry(target.x, target.y, target.level)
    const hit = strict.requests.filter((request) => request.url.endsWith('.terrain'))
    // 这条断言看着平凡，实际是 slippyMap/tms 之争的唯一可观测面：翻一次 y 就会打到另一张瓦上，
    // 于是"烘了 A 却拿到 B"——字节合法、几何错位，渲染端只会觉得地形莫名其妙。
    expect(hit.map((request) => request.url)).toEqual([`/terrain/${target.level}/${target.x}/${target.y}.terrain`])
    expect(hit[0]!.accept).toContain('extensions=octvertexnormals')
  })

  it('烘焙覆盖与 layer.json 的可用范围互相咬合：声明的每张瓦都在，深一层都判为不可用', async () => {
    expect(baked.length).toBeGreaterThan(0)
    for (const tile of baked) {
      expect(provider.availability!.isTileAvailable(tile.level, tile.x, tile.y), `L${tile.level} ${tile.x}/${tile.y} 应判为可用`).toBe(true)
      expect(provider.getTileDataAvailable(tile.x, tile.y, tile.level)).toBe(true)
    }
    // 超出烘焙深度必须回答"没有"。这是防止四叉树往下钻到不存在的层级：
    // 那儿的 404/兜底页会被当成瓦来解，最后表现为"地形整片不出"。
    const beyond = maxZoom + 1
    expect(provider.availability!.isTileAvailable(beyond, 0, 0)).toBe(false)
    expect(provider.getTileDataAvailable(0, 0, beyond)).toBe(false)
    // 完整金字塔的应有瓦数：层级 z 是 2^(z+1) × 2^z 张。少烘一张这里就红。
    const expected = Array.from({ length: maxZoom + 1 }, (_, level) => (2 << level) * (1 << level)).reduce((a, b) => a + b, 0)
    expect(baked.length).toBe(expected)
  })

  it('层级几何误差有限且严格递减（四叉树按它决定要不要继续下钻）', () => {
    const errors = Array.from({ length: maxZoom + 1 }, (_, level) => provider.getLevelMaximumGeometricError(level))
    for (const error of errors) expect(Number.isFinite(error)).toBe(true)
    for (let level = 1; level < errors.length; level += 1) {
      expect(errors[level]).toBeLessThan(errors[level - 1])
    }
  })

  it('八分球法向：客户端 opt-in 才解析，字节数正好是每顶点 2 字节', async () => {
    const target = baked.find((tile) => tile.level === maxZoom)!
    const decoded = decodeTerrainTile(await provider.requestTileGeometry(target.x, target.y, target.level))
    expect(provider.hasVertexNormals).toBe(true)
    expect(decoded.encodedNormalsBytes).toBe(decoded.vertexCount * 2)

    // 默认不请求法向时，解析器不去读那段扩展：这解释了"同一份字节，两次结果不同"，
    // 别让后来人把它当成资产坏了。
    const plain = await Cesium.CesiumTerrainProvider.fromUrl(`${strict.origin}/terrain`)
    const plainDecoded = decodeTerrainTile(await plain.requestTileGeometry(target.x, target.y, target.level))
    expect(plain.hasVertexNormals).toBe(false)
    expect(plainDecoded.encodedNormalsBytes).toBe(0)
    expect(plainDecoded.vertexCount).toBe(decoded.vertexCount)
  })

  it('每张烘焙瓦都解出自洽的几何：顶点/索引/四条边表/包围球/高度界', async () => {
    // 逐张真解析要跑完整套 HTTP + 解码，默认 5s 会随机红；这里给足预算但把话说清楚。
    const problems: string[] = []
    for (const tile of baked) {
      const data = await provider.requestTileGeometry(tile.x, tile.y, tile.level)
      if (data === undefined || data.constructor.name !== 'QuantizedMeshTerrainData') {
        problems.push(`L${tile.level}/${tile.x}/${tile.y} 没解成 QuantizedMeshTerrainData`)
        continue
      }
      const decoded = decodeTerrainTile(data)
      const edgeCounts = Object.values(decoded.edgeIndexCounts)
      if (
        decoded.vertexCount < 4 ||
        !Number.isInteger(decoded.triangleCount) ||
        decoded.triangleCount < 1 ||
        decoded.maxIndexRef >= decoded.vertexCount ||
        edgeCounts.some((count) => count === 0) ||
        !(decoded.minimumHeight < decoded.maximumHeight) ||
        !Number.isFinite(decoded.boundingSphereRadius) ||
        decoded.boundingSphereRadius <= 0
      ) {
        problems.push(`L${tile.level}/${tile.x}/${tile.y} 解出来的几何不自洽：${JSON.stringify(decoded)}`)
      }
      // 出处声明必须随瓦到达渲染路径：合成高程不能被当成真实 DEM（P0 的可辨识要求）。
      expect(decoded.creditHtml.join(' ')).toContain(PROVENANCE)
    }
    expect(problems, problems.join('\n')).toEqual([])
  }, 90_000)

  it('瓦的内容属于它自己那个矩形：高原瓦有起伏，而同一列镜像行的瓦没有', async () => {
    const scheme = provider.tilingScheme
    const plateauTile = scheme.positionToTileXY(PLATEAU, maxZoom, new Cesium.Cartesian2())
    expect(plateauTile).toBeDefined()
    const x = plateauTile!.x
    const y = plateauTile!.y
    const rectangle = scheme.tileXYToRectangle(x, y, maxZoom)
    expect(Cesium.Rectangle.contains(rectangle, PLATEAU)).toBe(true)

    const plateau = decodeTerrainTile(await provider.requestTileGeometry(x, y, maxZoom))
    expect(plateau.maximumHeight).toBeGreaterThan(4_000)

    // 同一列、行号镜像过去（slippyMap ↔ tms 的那个翻法）必须是一张几乎平的海/陆面。
    // 万一 y 口径在生成器或 layer.json 上被反过来，这里会先红，而不是等到浏览器里"地形对不上影像"。
    const mirroredY = scheme.getNumberOfYTilesAtLevel(maxZoom) - 1 - y
    const mirrored = decodeTerrainTile(await provider.requestTileGeometry(x, mirroredY, maxZoom))
    expect(mirrored.maximumHeight).toBeLessThan(1_000)
    expect(plateau.maximumHeight - mirrored.maximumHeight).toBeGreaterThan(3_000)

    // 最北那张同样是低值：证明"高"是跟着矩形来的，不是整批瓦的常数。
    const polar = decodeTerrainTile(await provider.requestTileGeometry(0, 0, maxZoom))
    expect(polar.maximumHeight).toBeLessThan(1_000)
  })

  it('出处声明与生成器一致，且随 layer.json 的 attribution 一起落盘', async () => {
    const layer = (await (await fetch(`${strict.origin}/terrain/layer.json`)).json()) as { attribution?: string; extensions?: string[]; scheme?: string; projection?: string }
    expect(layer.attribution).toBe(PROVENANCE)
    expect(layer.scheme).toBe('slippyMap')
    expect(layer.projection).toBe('EPSG:4326')
    expect(PROVENANCE).toContain('不是真实')
  })
})

describe('缺件不能被读成"有瓦"（三种失败都得响亮）', () => {
  /**
   * 浏览器实测：`vite preview` 对不存在的 `/terrain/3/0/0.terrain` 回 `200 text/html`，
   * Cesium 把这页 HTML 当二进制硬解 → `RangeError: Invalid typed array length`。
   * 老实 404 则是 `RequestErrorEvent`。两条都比"静默给一张空瓦"好，
   * 所以门禁要钉住的是：**任何一档都必须拒绝，绝不返回可渲染的瓦**。
   */
  async function requestMissing(missing: 'not-found' | 'spa-html'): Promise<{ outcome: string; error?: Error }> {
    const server = await startLocalAssetServer({ missing })
    try {
      const instance = await Cesium.CesiumTerrainProvider.fromUrl(`${server.origin}/terrain`)
      try {
        const data = await instance.requestTileGeometry(0, 0, maxZoom + 1)
        return { outcome: data === undefined ? 'undefined' : `parsed ${data.constructor.name}` }
      } catch (error) {
        return { outcome: 'rejected', error: error as Error }
      }
    } finally {
      await server.close()
    }
  }

  it('深一层瓦在 404 与 SPA 兜底页两档下都被拒绝，且不会解出瓦', async () => {
    const notFound = await requestMissing('not-found')
    expect(notFound.outcome).toBe('rejected')

    const spaHtml = await requestMissing('spa-html')
    expect(spaHtml.outcome).toBe('rejected')
    // 兜底页这一档的错误名字很关键：它证明字节真的被喂进了 quantized-mesh 解析器。
    expect(String(spaHtml.error?.message)).toMatch(/typed array length|out of bounds|RangeError/)
  })

  it('layer.json 声明可用但瓦不在位时，请求失败而不是静默出平地形', async () => {
    // 只拷 layer.json、不拷瓦：这就是"烘焙跑了一半/部署漏目录"的现场形状。
    const stage = await mkdtemp(join(tmpdir(), 'aegis-terrain-missing-'))
    await mkdir(join(stage, 'terrain'), { recursive: true })
    await copyFile(join(PUBLIC_DIR, 'terrain', 'layer.json'), join(stage, 'terrain', 'layer.json'))
    const server = await startLocalAssetServer({ missing: 'not-found', publicDir: stage })
    try {
      const instance = await Cesium.CesiumTerrainProvider.fromUrl(`${server.origin}/terrain`)
      // 声明说"有"，可用性就报"有"——问题只会在真取瓦时暴露，所以这一步必须红。
      expect(instance.availability!.isTileAvailable(0, 0, 0)).toBe(true)
      await expect(instance.requestTileGeometry(0, 0, 0)).rejects.toThrow()
    } finally {
      await server.close()
      await rm(stage, { recursive: true, force: true })
    }
  })

  it('Cesium 解析结果的字段名没有漂移（无头门禁读的就是这些私有字段）', () => {
    const dataFlat = cesiumSourceFlat('Core', 'QuantizedMeshTerrainData.js')
    const providerFlat = cesiumSourceFlat('Core', 'CesiumTerrainProvider.js')
    // 两侧都要核对：构造函数字段（读的一端）和 provider 传进来的同名 option（喂的一端）。
    // 只查一侧的话，字段还在但没人赋值也会照样读到 undefined，门禁就成了摆设。
    const staleReads = quantizedMeshFieldContract.filter((field) => !dataFlat.includes(`this.${field} =`))
    expect(staleReads, `QuantizedMeshTerrainData 里已不这样赋值：${staleReads.join(', ')}`).toEqual([])
    const staleFeeds = quantizedMeshFieldContract
      .map((field) => field.replace(/^_/, ''))
      .filter((option) => !providerFlat.includes(`${option}:`))
    expect(staleFeeds, `CesiumTerrainProvider 已不再传这些 option：${staleFeeds.join(', ')}`).toEqual([])
    // y 口径前提：只有 tms 才翻 y。layer.json 写 slippyMap 这条决定就靠它撑着。
    expect(providerFlat).toContain('provider._scheme === "tms"')
    // childTileMask 来自可用性而不是文件字节：这条也顺便钉住，
    // 免得后来人去生成器里找"为什么没写子瓦掩码"。
    expect(providerFlat).toContain('provider.availability.computeChildMaskForTile(level, x, y)')
  })

  it('可用性判读：顶层瓦报有子瓦，`available` 最深层按叶子处理', () => {
    expect(provider.availability!.computeChildMaskForTile(0, 0, 0)).toBe(15)
    expect(provider.availability!.computeChildMaskForTile(maxZoom, 0, 0)).toBe(0)
    expect(provider.availability!.computeMaximumLevelAtPosition(PLATEAU)).toBe(maxZoom)
    expect(provider.availability!.computeBestAvailableLevelOverRectangle(Cesium.Rectangle.fromDegrees(85, 25, 95, 35))).toBe(maxZoom)
  })
})
