/**
 * 画布校验规则与后端节点契约的对账（跨端漂移门禁）。
 *
 * `utils/graph.ts` 里的 `NODE_SPECS` 是后端 `workflow/nodes.py` 的 `NodeSpec` 的**镜像**：
 * 画布靠它在提交前拦下"缺必填 / 写了未知参数 / 数值低于下限"。镜像一旦与真源不一致，
 * 症状是固定的一种——前端说这张图没问题，保存到后端被拒（或更坏：前端拦了合法的配置，
 * 画布上根本存不下去）。这类漂移人眼扫不出来，且两端各自看都自洽。
 *
 * 后端没有把 `NodeSpec` 暴露成 HTTP 契约里的可比较结构时，这里直接读它的源码；
 * 一旦读不到预期形状，**抛错而不是跳过**——静默跳过的门禁等于没有门禁。
 */

import { describe, expect, it } from 'vitest'

import { NODE_SPECS } from '@/utils/graph'
import { backendInternalNodeTypes, readRepoFile } from '@/testing/repoSource'

interface BackendSpec {
  required: string[]
  optional: string[]
  min: Record<string, number>
  category: string
  outputs: string[]
}

/** 按顶层逗号切分实参：字符串里的逗号与嵌套括号都不能算分隔符。 */
function splitArgs(source: string): string[] {
  const parts: string[] = []
  let depth = 0
  let quote = ''
  let current = ''
  for (const char of source) {
    if (quote) {
      current += char
      if (char === quote) quote = ''
      continue
    }
    if (char === '"' || char === "'") {
      quote = char
      current += char
      continue
    }
    if (char === '(' || char === '[' || char === '{') depth += 1
    else if (char === ')' || char === ']' || char === '}') depth -= 1
    if (char === ',' && depth === 0) {
      parts.push(current.trim())
      current = ''
      continue
    }
    current += char
  }
  if (current.trim()) parts.push(current.trim())
  return parts
}

function stringsIn(tuple: string | undefined): string[] {
  if (!tuple) return []
  return [...tuple.matchAll(/"([a-z_]+)"/g)].map((match) => match[1] as string).sort()
}

/** 逐个扫出 `NodeSpec(...)` 的实参文本：实参里有元组/字典，正则的括号匹配靠不住。 */
function nodeSpecCalls(text: string): string[] {
  const marker = 'NodeSpec('
  const calls: string[] = []
  let from = 0
  while (true) {
    const start = text.indexOf(marker, from)
    if (start < 0) return calls
    let depth = 1
    let index = start + marker.length
    let quote = ''
    while (index < text.length && depth > 0) {
      const char = text[index] as string
      if (quote) {
        if (char === quote) quote = ''
      } else if (char === '"' || char === "'") {
        quote = char
      } else if (char === '(') {
        depth += 1
      } else if (char === ')') {
        depth -= 1
      }
      index += 1
    }
    if (depth !== 0) throw new Error(`nodes.py 第 ${start} 字符起的 NodeSpec( 没有闭合：注册写法变了，门禁要跟着改`)
    calls.push(text.slice(start + marker.length, index - 1))
    from = index
  }
}

/** 从 `nodes.py` 里解析 `NodeSpec(名称, 说明, handler, 必填, 可选, 下限, ...)` 的实参。 */
function backendSpecs(): Record<string, BackendSpec> {
  const text = readRepoFile('backend', 'src', 'aegis', 'workflow', 'nodes.py')
  const found: Record<string, BackendSpec> = {}
  for (const call of nodeSpecCalls(text)) {
    const args = splitArgs(call)
    const name = (args[0] ?? '').replace(/"/g, '')
    if (!/^[a-z_]+$/.test(name)) continue
    // 关键字实参（category=/outputs=/internal=）会插进位置参数中间：先摘掉再对齐槽位，
    // 否则短写法 `NodeSpec(名称, 说明, handler, category=...)` 会把类别读成必填配置。
    const positional = args.filter((arg) => !/^[a-z_]+=/.test(arg))
    const min: Record<string, number> = {}
    for (const pair of (positional[5] ?? '').matchAll(/"([a-z_]+)":\s*([-\d.]+)/g))
      min[pair[1] as string] = Number(pair[2])
    const categoryArg = args.find((arg) => arg.startsWith('category='))
    if (!categoryArg) throw new Error(`${name} 没有 category=：后端 NodeSpec 的位置参数表变了，门禁要跟着改`)
    const outputsArg = args.find((arg) => arg.startsWith('outputs='))
    found[name] = {
      required: stringsIn(positional[3]),
      optional: stringsIn(positional[4]),
      min,
      category: (categoryArg.match(/"([a-z]+)"/)?.[1] ?? '') as string,
      outputs: stringsIn(outputsArg),
    }
  }
  if (Object.keys(found).length < 10) {
    throw new Error(
      `nodes.py 里只解析到 ${Object.keys(found).length} 个 NodeSpec：注册写法变了，门禁要跟着改（不是把断言删掉）`,
    )
  }
  return found
}

describe('NODE_SPECS 与后端 NodeSpec 逐字段对账', () => {
  const backend = backendSpecs()

  it('节点类型集合完全一致：两端各数一遍，多一个少一个都算漂移', () => {
    expect(Object.keys(NODE_SPECS).sort()).toEqual(Object.keys(backend).sort())
  })

  it('每个节点的必填/可选参数名与后端一致', () => {
    const drift: string[] = []
    for (const [name, spec] of Object.entries(backend)) {
      const mirror = NODE_SPECS[name as keyof typeof NODE_SPECS]
      if (!mirror) continue // 上一条用例已经报过"集合不一致"
      if (JSON.stringify([...mirror.required_config].sort()) !== JSON.stringify(spec.required)) {
        drift.push(
          `${name}.required_config: 前端=${JSON.stringify([...mirror.required_config].sort())} 后端=${JSON.stringify(spec.required)}`,
        )
      }
      if (JSON.stringify([...mirror.optional_config].sort()) !== JSON.stringify(spec.optional)) {
        drift.push(
          `${name}.optional_config: 前端=${JSON.stringify([...mirror.optional_config].sort())} 后端=${JSON.stringify(spec.optional)}`,
        )
      }
    }
    expect(drift, drift.join('\n')).toEqual([])
  })

  it('数值下限（min_config）也一致：只比参数名会漏掉"前端允许 0 而后端拒绝 0"', () => {
    const drift: string[] = []
    for (const [name, spec] of Object.entries(backend)) {
      const mirror = NODE_SPECS[name as keyof typeof NODE_SPECS]
      if (!mirror) continue
      if (JSON.stringify({ ...mirror.min_config }) !== JSON.stringify({ ...spec.min })) {
        drift.push(`${name}.min_config: 前端=${JSON.stringify(mirror.min_config)} 后端=${JSON.stringify(spec.min)}`)
      }
    }
    expect(drift, drift.join('\n')).toEqual([])
  })

  it('内部原语（internal）标记一致：节点面板挡不挡得住，取决于两端说的是不是同一句话', () => {
    const mirrored = Object.keys(NODE_SPECS)
      .filter((name) => NODE_SPECS[name as keyof typeof NODE_SPECS].internal === true)
      .sort()
    expect(mirrored).toEqual(backendInternalNodeTypes())
  })

  it('类别与产出键一致：面板分组、强调色与上游变量都读这几格', () => {
    const drift: string[] = []
    for (const [name, spec] of Object.entries(backend)) {
      const mirror = NODE_SPECS[name as keyof typeof NODE_SPECS]
      if (!mirror) continue
      if (mirror.category !== spec.category) {
        drift.push(`${name}.category: 前端=${mirror.category} 后端=${spec.category}`)
      }
      if (JSON.stringify([...mirror.outputs].sort()) !== JSON.stringify(spec.outputs)) {
        drift.push(
          `${name}.outputs: 前端=${JSON.stringify([...mirror.outputs].sort())} 后端=${JSON.stringify(spec.outputs)}`,
        )
      }
    }
    expect(drift, drift.join('\n')).toEqual([])
  })
})
