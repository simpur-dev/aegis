// 把 Cesium 的静态资源从 node_modules 落到 public/cesium，供 viewer.ts 设置的
// CESIUM_BASE_URL 同源读取。刻意不提交这些文件（见根 .gitignore）：它们是依赖产物，
// 且高原弱网站点需要的是"构建一次、离线可用"，不是每次拉仓库都带着 40MB 二进制。
import { cpSync, existsSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const frontend = resolve(here, '..')
const source = resolve(frontend, 'node_modules/cesium/Build/Cesium')
const target = resolve(frontend, 'public/cesium')
const DIRS = ['Workers', 'Assets', 'ThirdParty', 'Widgets']

if (!existsSync(source)) {
  console.error(`[prepare-cesium] 未找到 ${source}：请先安装依赖（npm install）`)
  process.exit(1)
}

mkdirSync(target, { recursive: true })
let copied = 0
for (const dir of DIRS) {
  const from = resolve(source, dir)
  if (!existsSync(from)) {
    console.error(`[prepare-cesium] Cesium 发行包缺少 ${dir}/，版本可能已变化`)
    process.exit(1)
  }
  cpSync(from, resolve(target, dir), { recursive: true })
  copied += 1
}
console.log(`[prepare-cesium] 已就位 ${copied}/${DIRS.length} 个资源目录 → public/cesium`)
