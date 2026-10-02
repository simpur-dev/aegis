/**
 * 把 Cesium 解析出来的 quantized-mesh 瓦读成一个可断言的形状。
 *
 * 为什么不能只用公开 API：`QuantizedMeshTerrainData` 在 1.145 上公开的是
 * `createMesh / interpolateHeight / childTileMask / canUpsample / waterMask / credits`，
 * 前两个要 `document`（createMesh 走 task processor，实测 `ReferenceError: document is not defined`），
 * 而 `interpolateHeight` 依赖 createMesh 建的 `_mesh`（实测返回 undefined）。
 * 格式级门禁要问的是"解析器从我们字节里读出了什么"，那答案就在这批字段上，所以这里读它们——
 * 并用 `quantizedMeshFieldContract` 对已安装的 Cesium 源码做漂移门禁：字段改名时要红，
 * 绝不要静默读到 undefined 再把它当成"瓦没问题"。
 */

export interface TerrainEdgeIndexCounts {
  readonly west: number
  readonly south: number
  readonly east: number
  readonly north: number
}

export interface DecodedTerrainTile {
  /** u/v/height 三分量各自的顶点数（= `_quantizedVertices.length / 3`）。 */
  readonly vertexCount: number
  readonly indexCount: number
  readonly triangleCount: number
  /** 三角形索引引用到的最大顶点号：必须 < vertexCount，否则是越界索引。 */
  readonly maxIndexRef: number
  readonly edgeIndexCounts: TerrainEdgeIndexCounts
  /** 八分球编码法向的字节数；客户端没 opt-in 时解析器不读扩展，此处为 0。 */
  readonly encodedNormalsBytes: number
  readonly minimumHeight: number
  readonly maximumHeight: number
  readonly boundingSphereRadius: number
  readonly horizonOcclusionPoint: readonly [number, number, number]
  readonly childTileMask: number
  readonly creditHtml: readonly string[]
}

/** `CesiumTerrainProvider` 写回解析结果时用的字段名（漂移门禁逐条核对）。 */
export const quantizedMeshFieldContract = [
  '_quantizedVertices',
  '_indices',
  '_minimumHeight',
  '_maximumHeight',
  '_boundingSphere',
  '_horizonOcclusionPoint',
  '_westIndices',
  '_southIndices',
  '_eastIndices',
  '_northIndices',
  '_encodedNormals',
] as const

export function decodeTerrainTile(raw: unknown): DecodedTerrainTile {
  if (raw === null || typeof raw !== 'object') {
    throw new TypeError(`解析结果不是对象，拿不到 quantized-mesh 字段：${String(raw)}`)
  }
  const source = raw as Record<string, unknown>
  const read = (name: (typeof quantizedMeshFieldContract)[number]): unknown => {
    if (!(name in source)) throw new TypeError(`解析结果缺少 ${name}：Cesium 的字段名或解析路径变了，先核对已安装源码再动断言`)
    return source[name]
  }
  const typedArrayLength = (name: string, value: unknown): number => {
    if (value === undefined || value === null) return 0
    if (ArrayBuffer.isView(value) && 'length' in value) return (value as { length: number }).length
    throw new TypeError(`${name} 期望是 TypedArray，实际是 ${Object.prototype.toString.call(value)}`)
  }

  const vertices = read('_quantizedVertices')
  const vertexLength = typedArrayLength('_quantizedVertices', vertices)
  if (vertexLength % 3 !== 0) throw new TypeError(`_quantizedVertices 长度 ${vertexLength} 不是 3 的倍数（u/v/height 应等长）`)
  const indices = read('_indices')
  const indexCount = typedArrayLength('_indices', indices)

  const minimumHeight = read('_minimumHeight')
  if (typeof minimumHeight !== 'number' || !Number.isFinite(minimumHeight)) {
    throw new TypeError(`_minimumHeight 期望是有限数：${String(minimumHeight)}`)
  }
  const maximumHeight = read('_maximumHeight')
  if (typeof maximumHeight !== 'number' || !Number.isFinite(maximumHeight)) {
    throw new TypeError(`_maximumHeight 期望是有限数：${String(maximumHeight)}`)
  }
  const sphere = read('_boundingSphere') as { radius?: unknown } | undefined
  if (typeof sphere?.radius !== 'number' || !Number.isFinite(sphere.radius) || sphere.radius <= 0) {
    throw new TypeError(`_boundingSphere.radius 不是正有限数：${String(sphere?.radius)}`)
  }
  const occlusion = read('_horizonOcclusionPoint') as { x?: unknown; y?: unknown; z?: unknown } | undefined
  if (typeof occlusion?.x !== 'number' || typeof occlusion?.y !== 'number' || typeof occlusion?.z !== 'number') {
    throw new TypeError(`_horizonOcclusionPoint 不是三维直角坐标：${JSON.stringify(occlusion)}`)
  }

  const edges = (['_westIndices', '_southIndices', '_eastIndices', '_northIndices'] as const).map((name) =>
    typedArrayLength(name, read(name)),
  )
  let maxIndexRef = -1
  if (ArrayBuffer.isView(indices)) {
    const asNumbers = indices as unknown as ArrayLike<number>
    for (let i = 0; i < asNumbers.length; i += 1) {
      const ref = asNumbers[i]
      if (ref > maxIndexRef) maxIndexRef = ref
    }
  }
  const credits = (source['credits'] ?? source['_credits']) as { html?: string }[] | undefined

  return {
    vertexCount: vertexLength / 3,
    indexCount,
    triangleCount: indexCount / 3,
    maxIndexRef,
    edgeIndexCounts: { west: edges[0], south: edges[1], east: edges[2], north: edges[3] },
    encodedNormalsBytes: typedArrayLength('_encodedNormals', read('_encodedNormals')),
    minimumHeight,
    maximumHeight,
    boundingSphereRadius: sphere.radius,
    horizonOcclusionPoint: [occlusion.x, occlusion.y, occlusion.z],
    childTileMask: (source as { childTileMask?: number }).childTileMask ?? 0,
    creditHtml: (credits ?? []).map((credit) => credit.html ?? ''),
  }
}

/**
 * quantized-mesh 的顶点量化上限。`65535` 是错的：Cesium 建网格的 worker 用 `const maxShort = 32767`，
 * 按 65535 反算会得出"高度只有头部跨度一半"这种**假故障**（我自己就在这上面误判过一次，
 * 所以这个数由用例对着已安装源码核，而不是靠记忆）。
 */
export const QUANTIZED_MESH_MAX_SHORT = 32767

export interface TileRectangleDegrees {
  readonly west: number
  readonly south: number
  readonly east: number
  readonly north: number
}

export interface DecodedVertex {
  readonly lonDeg: number
  readonly latDeg: number
  readonly heightMeters: number
  readonly uCode: number
  readonly vCode: number
}

/**
 * 把解析结果反算成"顶点在地球上的位置 + 高程"，与 Cesium 建网格时同一套换算。
 *
 * 口径只有两条，写死在这里而不是散进各用例：`u` 自西向东，`v` **自南向北**
 * （quantized-mesh 与 `createVerticesFromQuantizedTerrainMesh` 都是 v 向上）。
 * 反算口径错了不会崩，只会让"几何对不对"整片失真，所以这两条必须在门禁里可核。
 */
export function decodeVertexPositions(raw: unknown, rectangle: TileRectangleDegrees): DecodedVertex[] {
  if (raw === null || typeof raw !== 'object') throw new TypeError(`解析结果不是对象：${String(raw)}`)
  const source = raw as Record<string, unknown>
  const uint16 = (name: string): Uint16Array => {
    const value = source[name]
    if (!(value instanceof Uint16Array)) throw new TypeError(`${name} 期望是 Uint16Array，实际是 ${Object.prototype.toString.call(value)}`)
    return value
  }
  const heightOf = (name: string): number => {
    const value = source[name]
    if (typeof value !== 'number' || !Number.isFinite(value)) throw new TypeError(`${name} 期望是有限数：${String(value)}`)
    return value
  }
  const u = uint16('_uValues')
  const v = uint16('_vValues')
  const codes = uint16('_heightValues')
  if (u.length !== v.length || v.length !== codes.length) {
    throw new TypeError(`u/v/height 三个分量长度不一致：${u.length}/${v.length}/${codes.length}`)
  }
  const minimum = heightOf('_minimumHeight')
  const maximum = heightOf('_maximumHeight')
  const spanLon = rectangle.east - rectangle.west
  const spanLat = rectangle.north - rectangle.south

  // 不能用 `u.map(...)`：TypedArray 的 map 返回同种 TypedArray，对象会被静默压成 0，
  // 于是所有比较都变成 false，跨度停成 [Infinity, -Infinity]——门禁会看起来"资产坏了"。
  const vertices: DecodedVertex[] = []
  for (let index = 0; index < codes.length; index += 1) {
    vertices.push({
      lonDeg: rectangle.west + (u[index] / QUANTIZED_MESH_MAX_SHORT) * spanLon,
      latDeg: rectangle.south + (v[index] / QUANTIZED_MESH_MAX_SHORT) * spanLat,
      heightMeters: minimum + (codes[index] / QUANTIZED_MESH_MAX_SHORT) * (maximum - minimum),
      uCode: u[index],
      vCode: v[index],
    })
  }
  return vertices
}

/** 一个量化码对应的高度跨度：所有"应当相等"的断言都拿它当容差。 */
export function quantizationStep(raw: unknown): number {
  const decoded = decodeTerrainTile(raw)
  return (decoded.maximumHeight - decoded.minimumHeight) / QUANTIZED_MESH_MAX_SHORT
}
