/**
 * 修复 public/terrain 烘焙瓦片的三角形绕序。
 *
 * 背景：scripts/build_offline_tiles.py 的内部网格三角形按 (a,b,c)/(b,d,c) 输出，
 * 在 u 东 / v 北的参数平面里是顺时针；Cesium 球面背面剔除把顺时针面当背面，
 * 于是整片地形顶面被剔掉、只剩裙边墙（画面表现为"深色平板 + 瓦片边框管"）。
 * 本工具只翻绕序、不动高程：解码 → 翻转非退化三角形 → 按首次引用重新编号顶点
 * （high-water-mark 索引编码的硬要求）→ 重排 u/v/h 与 oct 法线 → 重编码写回。
 *
 * 幂等： already 逆时针（多数面积为正）的瓦片直接跳过，重复运行不会翻回去。
 * 用法：node scripts/repair-terrain-winding.mjs [--dry]
 */
import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(fileURLToPath(new URL('.', import.meta.url)), '..', 'public', 'terrain')
const dry = process.argv.includes('--dry')

function walk(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) out.push(...walk(p))
    else if (name.endsWith('.terrain')) out.push(p)
  }
  return out
}

const zigzagDecode = (z) => (z >> 1) ^ -(z & 1)
const zigzagEncode = (v) => (v << 1) ^ (v >> 31)
function deltaDecode(arr) {
  let prev = 0
  return arr.map((z) => {
    prev += zigzagDecode(z)
    return prev
  })
}
function deltaEncode(values) {
  let prev = 0
  return values.map((v) => {
    const code = zigzagEncode(v - prev)
    prev = v
    return code
  })
}
function hwmDecode(codes) {
  let highest = 0
  return codes.map((c) => {
    if (c === 0) return highest++
    return highest - c
  })
}
function hwmEncode(indices) {
  let highest = 0
  return indices.map((v) => {
    if (v === highest) {
      highest += 1
      return 0
    }
    if (v < highest) return highest - v
    throw new Error(`索引 ${v} 超前 high-water mark ${highest}`)
  })
}

let flipped = 0
let skipped = 0
for (const file of walk(root)) {
  const b = readFileSync(file)
  let o = 88 // 3d center + 2f minmax + 4d bounding sphere + 3d horizon
  const vc = b.readUInt32LE(o)
  o += 4
  const raw = []
  for (let i = 0; i < vc * 3; i += 1) {
    raw.push(b.readUInt16LE(o))
    o += 2
  }
  const u = deltaDecode(raw.slice(0, vc))
  const v = deltaDecode(raw.slice(vc, vc * 2))
  const h = deltaDecode(raw.slice(vc * 2))
  const tc = b.readUInt32LE(o)
  o += 4
  const codes = []
  for (let i = 0; i < tc * 3; i += 1) {
    codes.push(b.readUInt16LE(o))
    o += 2
  }
  const idx = hwmDecode(codes)
  const edges = []
  for (let side = 0; side < 4; side += 1) {
    const n = b.readUInt32LE(o)
    o += 4
    const e = []
    for (let i = 0; i < n; i += 1) {
      e.push(b.readUInt16LE(o))
      o += 2
    }
    edges.push(e)
  }
  const extId = b.readUInt8(o)
  o += 1
  const extLen = b.readUInt32LE(o)
  o += 4
  const normals = b.subarray(o, o + extLen)
  o += extLen
  if (o !== b.length || extId !== 1 || extLen !== vc * 2) {
    throw new Error(`${file}: 布局与预期不符 (o=${o} len=${b.length} ext=${extId}/${extLen})`)
  }

  const area = (t) => {
    const [a, c, d] = [idx[t * 3], idx[t * 3 + 1], idx[t * 3 + 2]]
    return (u[c] - u[a]) * (v[d] - v[a]) - (u[d] - u[a]) * (v[c] - v[a])
  }
  let pos = 0
  let neg = 0
  for (let t = 0; t < tc; t += 1) {
    const s = area(t)
    if (s > 0) pos += 1
    else if (s < 0) neg += 1
  }
  if (pos >= neg) {
    skipped += 1
    continue
  }

  const tris = []
  for (let t = 0; t < tc; t += 1) {
    const a = idx[t * 3]
    const c = idx[t * 3 + 1]
    const d = idx[t * 3 + 2]
    // 裙边墙在 u/v 投影里面积为零，绕序本来就对（今天能看见的就是它们），不动。
    tris.push(area(t) === 0 ? [a, c, d] : [a, d, c])
  }
  const remap = new Map()
  const flat = []
  for (const tri of tris) {
    for (const vid of tri) {
      if (!remap.has(vid)) remap.set(vid, remap.size)
      flat.push(remap.get(vid))
    }
  }
  if (remap.size !== vc) throw new Error(`${file}: 重编号后顶点数 ${remap.size} != ${vc}`)
  const reorder = (arr) => {
    const out = new Array(vc)
    for (const [oldId, newId] of remap) out[newId] = arr[oldId]
    return out
  }
  const nu = reorder(u)
  const nv = reorder(v)
  const nh = reorder(h)
  const nn = Buffer.alloc(vc * 2)
  for (const [oldId, newId] of remap) normals.copy(nn, newId * 2, oldId * 2, oldId * 2 + 2)

  const parts = [b.subarray(0, 88)]
  const pushU16 = (values) => {
    const buf = Buffer.alloc(values.length * 2)
    values.forEach((x, i) => buf.writeUInt16LE(x, i * 2))
    parts.push(buf)
  }
  const pushU32 = (x) => {
    const buf = Buffer.alloc(4)
    buf.writeUInt32LE(x, 0)
    parts.push(buf)
  }
  pushU32(vc)
  pushU16([...deltaEncode(nu), ...deltaEncode(nv), ...deltaEncode(nh)])
  pushU32(tc)
  pushU16(hwmEncode(flat))
  for (const e of edges) {
    pushU32(e.length)
    pushU16(e.map((x) => remap.get(x)))
  }
  parts.push(Buffer.from([extId]))
  pushU32(nn.length)
  parts.push(nn)
  const out = Buffer.concat(parts)
  if (out.length !== b.length) throw new Error(`${file}: 长度变化 ${b.length} -> ${out.length}`)
  if (!dry) writeFileSync(file, out)
  flipped += 1
}
console.log(`${dry ? '[dry] ' : ''}flipped=${flipped} skipped=${skipped}`)