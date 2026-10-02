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

/** 从 `backend/src/aegis/workflow/nodes.py` 里解析出注册过的节点类型名（`NodeSpec("name", …)`）。 */
export function backendNodeTypes(): string[] {
  const text = readRepoFile('backend', 'src', 'aegis', 'workflow', 'nodes.py')
  const found = [...text.matchAll(/NodeSpec\(\s*"([a-z_]+)"/g)].map((match) => match[1])
  if (found.length === 0) {
    throw new Error('nodes.py 里一个 NodeSpec 都没解析到：注册写法变了，门禁得跟着改（不是把断言删掉）')
  }
  return [...new Set(found)].sort()
}

/** 从装配面源码里解析出对外报出的腿名（状态行的唯一真源：`IntegrationState(name=...)`）。 */
export function backendIntegrationLegs(): string[] {
  const found = new Set<string>()
  for (const file of ['integrations.py', 'container.py']) {
    const text = readRepoFile('backend', 'src', 'aegis', file)
    for (const match of text.matchAll(/IntegrationState\(\s*name="([a-z_]+)"/g)) found.add(match[1] as string)
  }
  if (found.size === 0) {
    throw new Error('装配面里一个 IntegrationState 都没解析到：写法变了，门禁得跟着改（不是把断言删掉）')
  }
  return [...found]
}
