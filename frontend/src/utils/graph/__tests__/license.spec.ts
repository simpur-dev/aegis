import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative, sep } from 'node:path'
import { describe, expect, it } from 'vitest'

function findRepoRoot(start: string): string {
  let current = start
  while (true) {
    if (existsSync(join(current, 'docs', 'audit', 'DIFY_MIGRATION_INVENTORY.md'))) return current
    const parent = dirname(current)
    if (parent === current) throw new Error('找不到仓库根目录（docs/audit/DIFY_MIGRATION_INVENTORY.md）')
    current = parent
  }
}

const REPO_ROOT = findRepoRoot(process.cwd())
const SRC_ROOT = join(REPO_ROOT, 'frontend', 'src')
const INVENTORY = join(REPO_ROOT, 'docs', 'audit', 'DIFY_MIGRATION_INVENTORY.md')
const DIFY_COMMIT = '8387590ace4a094de812b7847fc6a4c3a27cd52b'
const DIFY_TAG = '1.17.1'
const MARKER = ['Ported', 'from', 'Dify', 'web/app/components/workflow/'].join(' ')
const BANNED_KIND = ['dify', 'workflow', 'clipboard'].join('-')

export function hasDifyAttribution(source: string): boolean {
  const head = source.split(/\r?\n/, 6).join('\n')
  return (
    head.includes(MARKER) &&
    head.includes(`tag ${DIFY_TAG}`) &&
    head.includes(DIFY_COMMIT) &&
    head.includes('Copyright (c) LangGenius, Inc.') &&
    head.includes('modified Apache License 2.0') &&
    head.includes('THIRD_PARTY_NOTICES.md')
  )
}

function inventoryLandingPaths(markdown: string): Set<string> {
  const paths = new Set<string>()
  for (const line of markdown.split(/\r?\n/)) {
    if (!/^\|\s*T[12]-\d+/.test(line)) continue
    for (const match of line.matchAll(/`(frontend\/src\/[^`]+)`/g)) paths.add(match[1])
  }
  return paths
}

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === '__fixtures__' || entry === '__tests__') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (/\.(ts|vue)$/.test(entry)) out.push(full)
  }
  return out
}

describe('Dify 迁移许可守卫', () => {
  it('归属头检查器自身可判别缺失与合规', () => {
    expect(hasDifyAttribution('export const x = 1')).toBe(false)
    expect(hasDifyAttribution(`${MARKER}utils/version.ts\n// (tag 1.17.1, commit deadbeef).`)).toBe(false)
    expect(
      hasDifyAttribution(
        [
          `// ${MARKER}utils/version.ts`,
          `// (tag ${DIFY_TAG}, commit ${DIFY_COMMIT}).`,
          '// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.',
          '// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.',
          'export const x = 1',
        ].join('\n'),
      ),
    ).toBe(true)
  })

  it('inventory 至少登记了 T1/T2 落点', () => {
    const inventory = readFileSync(INVENTORY, 'utf8')
    expect(inventoryLandingPaths(inventory).size).toBeGreaterThan(0)
  })

  it('已落盘的迁移文件必须有完整归属头', () => {
    const inventory = readFileSync(INVENTORY, 'utf8')
    const listed = inventoryLandingPaths(inventory)
    const missingHeaders: string[] = []

    for (const rel of listed) {
      const full = join(REPO_ROOT, rel)
      let source: string
      try {
        source = readFileSync(full, 'utf8')
      } catch {
        continue
      }
      if (!hasDifyAttribution(source)) missingHeaders.push(rel)
    }

    expect(missingHeaders).toEqual([])
  })

  it('带归属头的文件必须出现在 inventory（反向一致）', () => {
    const inventory = readFileSync(INVENTORY, 'utf8')
    const listed = inventoryLandingPaths(inventory)
    const unregistered: string[] = []

    for (const full of walk(SRC_ROOT)) {
      if (!readFileSync(full, 'utf8').includes(MARKER)) continue
      const rel = relative(REPO_ROOT, full).split(sep).join('/')
      if (!listed.has(rel)) unregistered.push(rel)
    }

    expect(unregistered).toEqual([])
  })

  it('生产代码不得出现 Dify 剪贴板 kind', () => {
    const offenders: string[] = []
    for (const full of walk(SRC_ROOT)) {
      if (full.endsWith('.spec.ts')) continue
      if (readFileSync(full, 'utf8').includes(BANNED_KIND)) offenders.push(relative(REPO_ROOT, full))
    }
    expect(offenders).toEqual([])
  })
})
