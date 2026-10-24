/**
 * 分层架构的 AST 守卫：ESLint 的 `no-restricted-imports` 只看它被喂进去的文件，
 * 而 CI 脚本的 glob 一旦漏掉某个目录，规则会安静地失效。这里从仓库里自己找文件、
 * 自己解析 import，断言 domain / composables / stores / components / transport / view 六层
 * 各自不得越过下面那张表。
 *
 * `.vue` 的 `<script>` 也按 TypeScript 解析：ESLint 覆盖不到的组合（新目录、新后缀）
 * 在这里仍有第二道，而不是靠"CI 里应该配了"。
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, sep } from 'node:path'

import * as ts from 'typescript'
import { describe, expect, it } from 'vitest'

import { readRepoFile, repoRoot } from '@/testing/repoSource'

const FRONTEND_ROOT = join(repoRoot(), 'frontend')
const SRC_ROOT = join(FRONTEND_ROOT, 'src')
const VIEW_FILE = join(SRC_ROOT, 'views', 'WorkflowView.vue')

const WORKFLOW_ROOTS = [
  join(SRC_ROOT, 'utils', 'graph'),
  join(SRC_ROOT, 'composables', 'workflow'),
  join(SRC_ROOT, 'stores', 'workflow'),
  join(SRC_ROOT, 'api', 'workflow'),
  join(SRC_ROOT, 'components', 'workflow'),
] as const

type Layer = 'domain' | 'composables' | 'stores' | 'components' | 'transport' | 'client' | 'view'

interface LayerRule {
  readonly layer: Layer
  readonly label: string
  readonly forbidden: readonly string[]
}

const RULES: readonly LayerRule[] = [
  {
    layer: 'domain',
    label: 'domain 层（utils/graph）',
    forbidden: [
      'vue',
      'vue-router',
      'pinia',
      '@vueuse/core',
      'axios',
      '@vue-flow/*',
      'ant-design-vue',
      '@/api/**',
      '@/stores/**',
      '@/components/**',
      '@/composables/**',
      '@/views/**',
    ],
  },
  {
    layer: 'composables',
    label: 'composables 层',
    forbidden: ['axios', '@/stores/**', '@/components/**', '@/views/**', '@/api/workflow/client'],
  },
  {
    layer: 'stores',
    label: 'store 层',
    forbidden: ['axios', '@/components/**', '@/views/**', '@/api/workflow/client'],
  },
  {
    layer: 'components',
    label: 'workflow 组件层',
    forbidden: ['axios', '@/api/workflow/client'],
  },
  {
    layer: 'transport',
    label: '传输层（contract/codec/index）',
    forbidden: ['axios', 'vue', 'vue-router', 'pinia', '@/stores/**', '@/components/**', '@/views/**'],
  },
  {
    layer: 'client',
    label: '唯一 axios 出口（client.ts）',
    forbidden: ['vue', 'vue-router', 'pinia', '@/stores/**', '@/components/**', '@/views/**'],
  },
  {
    layer: 'view',
    label: '编排页（WorkflowView）',
    forbidden: ['axios', '@/api/workflow/client'],
  },
]

function layerOf(relPath: string): Layer | null {
  if (relPath === 'src/views/WorkflowView.vue') return 'view'
  if (relPath === 'src/api/workflow/client.ts') return 'client'
  if (relPath.startsWith('src/utils/graph/')) return 'domain'
  if (relPath.startsWith('src/composables/workflow/')) return 'composables'
  if (relPath.startsWith('src/stores/workflow/')) return 'stores'
  if (relPath.startsWith('src/api/workflow/')) return 'transport'
  if (relPath.startsWith('src/components/workflow/')) return 'components'
  return null
}

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === '__tests__' || entry === '__fixtures__' || entry === 'node_modules') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (/\.(ts|vue)$/.test(entry)) out.push(full)
  }
  return out
}

/** SFC 只把 `<script>` / `<script setup>` 的内容交给 TS 解析；template 里没有 import 语义。 */
function scriptSource(path: string, source: string): string {
  if (!path.endsWith('.vue')) return source
  return [...source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)].map((match) => match[1] ?? '').join('\n')
}

/**
 * 所有静态与动态模块引用：`import` / `export … from` / `import x = require()` / `import('…')`。
 * 只认字面量；未来的计算式动态 import 会被这里漏掉，但那种写法本身就该在评审时拦下。
 */
export function extractModuleSpecifiers(source: string, fileName = 'source.ts'): string[] {
  const file = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
  const found: string[] = []
  const visit = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
      if (node.moduleSpecifier !== undefined && ts.isStringLiteral(node.moduleSpecifier)) {
        found.push(node.moduleSpecifier.text)
      }
    } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
      const target = node.moduleReference.expression
      if (ts.isStringLiteral(target)) found.push(target.text)
    } else if (
      ts.isCallExpression(node) &&
      node.expression.kind === ts.SyntaxKind.ImportKeyword &&
      node.arguments.length === 1 &&
      ts.isStringLiteral(node.arguments[0] as ts.Expression)
    ) {
      found.push((node.arguments[0] as ts.StringLiteral).text)
    } else if (
      ts.isImportTypeNode(node) &&
      ts.isLiteralTypeNode(node.argument) &&
      ts.isStringLiteral(node.argument.literal)
    ) {
      found.push(node.argument.literal.text)
    }
    ts.forEachChild(node, visit)
  }
  visit(file)
  return found
}

export function matchesSpecifier(specifier: string, pattern: string): boolean {
  if (pattern.endsWith('/**')) {
    const base = pattern.slice(0, -3)
    return specifier === base || specifier.startsWith(`${base}/`)
  }
  if (pattern.endsWith('/*')) return specifier.startsWith(pattern.slice(0, -1))
  return specifier === pattern || specifier.startsWith(`${pattern}/`)
}

interface ScanResult {
  readonly path: string
  readonly relPath: string
  readonly layer: Layer | null
  readonly source: string
  readonly specifiers: readonly string[]
}

function scan(): ScanResult[] {
  const files = [...WORKFLOW_ROOTS.flatMap((root) => walk(root)), VIEW_FILE]
  return files.map((path) => {
    const source = readFileSync(path, 'utf-8')
    const relPath = relative(FRONTEND_ROOT, path).split(sep).join('/')
    return {
      path,
      relPath,
      layer: layerOf(relPath),
      source,
      specifiers: extractModuleSpecifiers(scriptSource(path, source), relPath),
    }
  })
}

const SCANNED = scan()

describe('分层守卫的扫描面与匹配器', () => {
  it('每个 workflow 根目录都真的扫到了源码（glob 漏配会让守卫空转）', () => {
    for (const root of WORKFLOW_ROOTS) {
      const rel = relative(FRONTEND_ROOT, root).split(sep).join('/')
      expect(
        SCANNED.some((file) => file.relPath.startsWith(`${rel}/`)),
        `${rel} 下一个文件都没扫到`,
      ).toBe(true)
    }
    expect(SCANNED.some((file) => file.relPath === 'src/views/WorkflowView.vue')).toBe(true)
  })

  it('import 提取器认得静态、再导出、require、动态 import 与 import 类型', () => {
    const sample = [
      "import type { Ref } from 'vue'",
      "export { useThing } from '@/stores/workflow/editor'",
      "import legacy = require('pinia')",
      "const lazy = import('axios')",
      "type Edge = import('@vue-flow/core').Edge",
    ].join('\n')
    expect(extractModuleSpecifiers(sample).sort()).toEqual(
      ['@/stores/workflow/editor', '@vue-flow/core', 'axios', 'pinia', 'vue'].sort(),
    )
  })

  it('匹配器按包边界与目录前缀判定，不做子串误伤', () => {
    expect(matchesSpecifier('@/api/workflow/contract', '@/api/**')).toBe(true)
    expect(matchesSpecifier('@/api', '@/api/**')).toBe(true)
    expect(matchesSpecifier('@/apiworkflow', '@/api/**')).toBe(false)
    expect(matchesSpecifier('@vue-flow/core', '@vue-flow/*')).toBe(true)
    expect(matchesSpecifier('@vue-flowish/core', '@vue-flow/*')).toBe(false)
    expect(matchesSpecifier('vue-router', 'vue')).toBe(false)
    expect(matchesSpecifier('vue/server-renderer', 'vue')).toBe(true)
  })
})

describe('分层 import 图', () => {
  for (const rule of RULES) {
    it(`${rule.label}不得越过自己的边界`, () => {
      const offenders: string[] = []
      for (const file of SCANNED) {
        if (file.layer !== rule.layer) continue
        for (const specifier of file.specifiers) {
          const hit = rule.forbidden.find((pattern) => matchesSpecifier(specifier, pattern))
          if (hit !== undefined) offenders.push(`${file.relPath} → ${specifier}（命中 ${hit}）`)
        }
      }
      expect(offenders).toEqual([])
    })
  }

  it('axios 只能从 client.ts 进入，UI/store 不得直连', () => {
    const offenders = SCANNED.filter(
      (file) => file.relPath !== 'src/api/workflow/client.ts' && file.specifiers.includes('axios'),
    ).map((file) => file.relPath)
    expect(offenders).toEqual([])
    const client = SCANNED.find((file) => file.relPath === 'src/api/workflow/client.ts')
    expect(client?.specifiers, 'client.ts 不再 import axios，这条守卫的对照面就空了').toContain('axios')
  })

  it('整个 workflow 面只有 client.ts 能 axios.create', () => {
    const offenders = SCANNED.filter(
      (file) => file.relPath !== 'src/api/workflow/client.ts' && /\baxios\s*\.\s*create\s*\(/.test(file.source),
    ).map((file) => file.relPath)
    expect(offenders).toEqual([])
  })
})

describe('CI 与覆盖率门禁的接线', () => {
  it('CI 前端 job 跑 lint:workflow / format:check:workflow / test:coverage', () => {
    const ci = readRepoFile('.github', 'workflows', 'ci.yml')
    expect(ci).toContain('npm run lint:workflow')
    expect(ci).toContain('npm run format:check:workflow')
    expect(ci).toContain('npm run test:coverage')
  })

  it('两个 workflow 脚本覆盖全部五个目录与编排页，不再只盯 domain', () => {
    const pkg = JSON.parse(readRepoFile('frontend', 'package.json')) as {
      scripts: Record<string, string>
    }
    for (const script of ['lint:workflow', 'format:check:workflow']) {
      const command = pkg.scripts[script] ?? ''
      for (const dir of [
        'utils/graph',
        'composables/workflow',
        'stores/workflow',
        'api/workflow',
        'components/workflow',
      ]) {
        expect(command, `${script} 漏了 ${dir}`).toContain(`src/${dir}/**`)
      }
      expect(command, `${script} 漏了编排页`).toContain('src/views/WorkflowView.vue')
    }
  })

  it('覆盖率阈值仍钉在 90/85，且统计面包含四个新层', () => {
    const config = readRepoFile('frontend', 'vite.config.ts')
    expect(config).toMatch(/lines:\s*90/)
    expect(config).toMatch(/functions:\s*90/)
    expect(config).toMatch(/statements:\s*90/)
    expect(config).toMatch(/branches:\s*85/)
    for (const dir of ['utils/graph', 'composables/workflow', 'stores/workflow']) {
      expect(config).toContain(`'src/${dir}/**/*.ts'`)
    }
    expect(config).toContain("'src/api/workflow/codec.ts'")
  })
})
