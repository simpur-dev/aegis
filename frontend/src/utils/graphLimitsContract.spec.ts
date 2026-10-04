/**
 * 画布取值域与后端源码对账（跨端漂移门禁）。
 *
 * `utils/graph.ts` 里那批常量是**抄来的**：抄错一个 0，症状就不是"报错"而是
 * "前端放过去了、后端 422"或者反过来"前端把合法值夹住了、用户以为是 bug"。
 * 抄来的东西必须由另一端自己来证明，所以这里不写死期望值，直接读
 * `workflow_api.py`（写接口）与 `model.py`（RetryPolicy）比对。
 *
 * 解析不到约束时抛错：门禁宁可比着源码喊"我读不懂"，也不要空跑成绿色。
 */

import { describe, expect, it } from 'vitest'

import { readRepoFile } from '@/testing/repoSource'
import {
  MAX_ATTEMPTS,
  MAX_BACKOFF_MS,
  MAX_DEFINITION_NAME_CHARS,
  MAX_DESCRIPTION_CHARS,
  MAX_EDGES,
  MAX_NODE_NAME_CHARS,
  MAX_NODES,
  MIN_DEFINITION_NAME_CHARS,
  NODE_ID_PATTERN,
  NODE_TYPE_PATTERN,
  SLA_MAX_MS,
  SLA_MIN_MS,
} from '@/utils/graph'

const API = readRepoFile('backend', 'src', 'aegis', 'api', 'workflow_api.py')
const MODEL = readRepoFile('backend', 'src', 'aegis', 'workflow', 'model.py')

function classBlock(source: string, className: string): string {
  const start = source.indexOf(`class ${className}(`)
  if (start < 0) throw new Error(`${className} 不在了：类名改了，门禁要跟着改（不是把断言删掉）`)
  const rest = source.slice(start + 5)
  const next = rest.search(/\nclass /)
  return next < 0 ? rest : rest.slice(0, next)
}

function fieldLine(source: string, className: string, field: string): string {
  const block = classBlock(source, className)
  const match = block.match(new RegExp(`^[ \\t]+${field}: .*$`, 'm'))
  if (match === null) throw new Error(`后端 ${className} 里没有 ${field} 这一格：口径变了`)
  return match[0]
}

function intOf(line: string, key: 'ge' | 'le' | 'min_length' | 'max_length'): number {
  const match = line.match(new RegExp(`${key}=(\\d[\\d_]*)`))
  if (match === null) throw new Error(`那行没写 ${key}：${line.trim()}`)
  return Number(match[1]?.replace(/_/g, ''))
}

function patternOf(line: string): string {
  const match = line.match(/pattern=r?"([^"]+)"/)
  if (match === null) throw new Error(`那行没写 pattern：${line.trim()}`)
  return match[1] as string
}

describe('画布取值域与后端一致', () => {
  it('节点/连线数量上限取自 DefinitionInput 的 min/max_length', () => {
    expect(MAX_NODES).toBe(intOf(fieldLine(API, 'DefinitionInput', 'nodes'), 'max_length'))
    expect(MAX_EDGES).toBe(intOf(fieldLine(API, 'DefinitionInput', 'edges'), 'max_length'))
  })

  it('SLA 与超时的上下界取自 NodeInput（两者同一条口径）', () => {
    for (const field of ['sla_ms', 'timeout_ms']) {
      const line = fieldLine(API, 'NodeInput', field)
      expect(SLA_MIN_MS, field).toBe(intOf(line, 'ge'))
      expect(SLA_MAX_MS, field).toBe(intOf(line, 'le'))
    }
  })

  it('重试次数与退避上界取自 RetryPolicy', () => {
    expect(MAX_ATTEMPTS).toBe(intOf(fieldLine(MODEL, 'RetryPolicy', 'max_attempts'), 'le'))
    expect(MAX_BACKOFF_MS).toBe(intOf(fieldLine(MODEL, 'RetryPolicy', 'backoff_ms'), 'le'))
  })

  it('节点号与节点类型的正则与后端同一条', () => {
    expect(NODE_ID_PATTERN.source).toBe(patternOf(fieldLine(API, 'NodeInput', 'node_id')))
    expect(NODE_TYPE_PATTERN.source).toBe(patternOf(fieldLine(API, 'NodeInput', 'type')))
  })

  it('节点显示名的长度上限也一致（编辑器里 slice 与 maxlength 的那个数）', () => {
    expect(MAX_NODE_NAME_CHARS).toBe(intOf(fieldLine(API, 'NodeInput', 'name'), 'max_length'))
  })

  it('流程名与说明的长度界取自 DefinitionInput', () => {
    const name = fieldLine(API, 'DefinitionInput', 'name')
    expect(MIN_DEFINITION_NAME_CHARS).toBe(intOf(name, 'min_length'))
    expect(MAX_DEFINITION_NAME_CHARS).toBe(intOf(name, 'max_length'))
    expect(MAX_DESCRIPTION_CHARS).toBe(intOf(fieldLine(API, 'DefinitionInput', 'description'), 'max_length'))
  })
})
