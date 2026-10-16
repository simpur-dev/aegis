/**
 * 测试期读仓库源码用的定位工具：跨端漂移门禁要靠它把"另一端的真源"读进来，
 * 而不是把对方的清单抄一份到测试里——抄来的清单只能证明"两份抄写一致"，
 * 证明不了"另一端没变"。
 *
 * 为什么不用 `import.meta.url`：vitest 里它对源码文件可能是 `/@fs/...` 虚拟路径，
 * 直接拿去 `readFileSync` 会失败；而 `process.cwd()` 在 vitest 下就是 `frontend/`，
 * 于是从 cwd 往上找一个含 `backend/src/aegis` 的目录当仓库根，两种环境都成立。
 */

import { readFileSync } from 'node:fs'
import { existsSync } from 'node:fs'
import { readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'

const MAX_UPWARDS = 6

export function repoRoot(start: string = process.cwd()): string {
  let dir = resolve(start)
  for (let depth = 0; depth <= MAX_UPWARDS; depth += 1) {
    if (existsSync(join(dir, 'backend', 'src', 'aegis'))) return dir
    const parent = resolve(dir, '..')
    if (parent === dir) break
    dir = parent
  }
  throw new Error(`没找到仓库根（应向包含 backend/src/aegis 的那一层），当前从 ${start} 往上 ${MAX_UPWARDS} 层都没命中`)
}

/** 读仓库内某个文件的文本；路径按仓库根解析，缺文件就响亮报错而不是静默跳过门禁。 */
export function readRepoFile(...parts: string[]): string {
  const target = join(repoRoot(), ...parts)
  if (!existsSync(target)) throw new Error(`跨端门禁要读的文件不在位：${target}`)
  return readFileSync(target, 'utf-8')
}

/** 从 `backend/src/aegis/workflow/nodes.py` 里解析出注册过的节点类型名（`NodeSpec("name", …）`）。 */
export function backendNodeTypes(): string[] {
  return backendSpecFlags()
    .map((entry) => entry.type)
    .sort()
}

/** 后端 `NodeSpec.internal` 为真的那几类：引擎能驱动，但 `/node-types` 与节点面板都不给看。 */
export function backendInternalNodeTypes(): string[] {
  return backendSpecFlags()
    .filter((entry) => entry.internal)
    .map((entry) => entry.type)
    .sort()
}

/** 面板口径的注册集合（= 全部类型减去内部原语）。 */
export function backendPanelNodeTypes(): string[] {
  const internal = new Set(backendInternalNodeTypes())
  return backendNodeTypes().filter((type) => !internal.has(type))
}

interface BackendSpecFlag {
  readonly type: string
  readonly internal: boolean
}

/** 逐个 `NodeSpec(...)` 调用扫一遍：类型名取第一个实参，`internal` 写在同一调用的关键字实参里。 */
function backendSpecFlags(): BackendSpecFlag[] {
  const text = readRepoFile('backend', 'src', 'aegis', 'workflow', 'nodes.py')
  const found: BackendSpecFlag[] = []
  for (const match of text.matchAll(/NodeSpec\(\s*"([a-z_]+)"/g)) {
    const start = (match.index ?? 0) + match[0].length
    // 括号深度扫描到本次调用的右括号为止：`internal=True` 是写在调用尾部的，
    // 只看第一个 `)` 会把它落在窗外（那些元组实参本身就带括号）。字符串里的括号跳过。
    let depth = 1
    let index = start
    let quote = ''
    while (index < text.length && depth > 0) {
      const char = text[index] as string
      if (quote) {
        if (char === quote) quote = ''
      } else if (char === '"' || char === "'") quote = char
      else if (char === '(') depth += 1
      else if (char === ')') depth -= 1
      index += 1
    }
    found.push({ type: match[1] as string, internal: /internal\s*=\s*True/.test(text.slice(start, index)) })
  }
  if (found.length === 0) {
    throw new Error('nodes.py 里一个 NodeSpec 都没解析到：注册写法变了，门禁得跟着改（不是把断言删掉）')
  }
  return found
}

/** 从装配面源码里解析出对外报出的腿名（状态行的唯一真源：`IntegrationState(name=...)`）。
 *
 * 装配面从 2026-10-16 起是一个包（`backend/src/aegis/integrations/*.py`，P2 拆分），
 * 所以这里扫目录而不是盯某一个文件名——盯文件名的门禁在搬家后会静默读到零个装配行。
 */
export function backendIntegrationLegs(): string[] {
  const found = new Set<string>()
  const root = repoRoot()
  const assemblyDir = join(root, 'backend', 'src', 'aegis', 'integrations')
  const files = readdirSync(assemblyDir)
    .filter((name) => name.endsWith('.py'))
    .map((name) => join(assemblyDir, name))
  if (files.length === 0) throw new Error(`装配包目录是空的（或不存在）：${assemblyDir}`)
  const texts = [...files, join(root, 'backend', 'src', 'aegis', 'container.py')].map((file) => readFileSync(file, 'utf-8'))
  for (const text of texts) {
    for (const match of text.matchAll(/IntegrationState\(\s*name="([a-z_]+)"/g)) found.add(match[1] as string)
  }
  if (found.size === 0) {
    throw new Error('装配面里一个 IntegrationState 都没解析到：写法变了，门禁得跟着改（不是把断言删掉）')
  }
  return [...found]
}
