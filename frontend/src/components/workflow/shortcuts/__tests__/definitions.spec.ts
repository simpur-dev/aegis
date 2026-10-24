import { describe, expect, it } from 'vitest'

import shortcutsFixture from '@/utils/graph/__fixtures__/dify-1.17.1/shortcuts.json'

import {
  getWorkflowCanvasShortcutDisplayKey,
  WORKFLOW_CANVAS_SHORTCUT_GROUPS,
  WORKFLOW_CANVAS_SHORTCUTS,
  type WorkflowCanvasShortcutDefinition,
  type WorkflowCanvasShortcutKey,
} from '../definitions'

interface UpstreamRow {
  id: string
  hotkeys: string[] | null
  holdKey: string | null
  displayHotkey: string | null
  displayKey: string
  name: string
}

const upstreamRows = shortcutsFixture.upstream as unknown as UpstreamRow[]
const allKeys = Object.keys(WORKFLOW_CANVAS_SHORTCUTS) as WorkflowCanvasShortcutKey[]

describe('快捷键表（Dify T1 端口）', () => {
  it('保留子集与 fixture 的 aegisPlan 完全一致（不多不少）', () => {
    expect([...allKeys].sort()).toEqual(
      [
        ...shortcutsFixture.aegisPlan.retained,
        'workflow.cut',
        'workflow.help',
        'workflow.locate',
        'workflow.save',
      ].sort(),
    )
  })

  it('被排除的三条确实没有落盘', () => {
    for (const excluded of shortcutsFixture.aegisPlan.excluded) {
      expect(allKeys).not.toContain(excluded)
    }
  })

  it('保留项的按键与展示键逐条与上游一致', () => {
    for (const row of upstreamRows) {
      const retained = shortcutsFixture.aegisPlan.retained.includes(row.id)
      if (!retained) continue

      const key = row.id as WorkflowCanvasShortcutKey
      const definition: WorkflowCanvasShortcutDefinition = WORKFLOW_CANVAS_SHORTCUTS[key]
      expect('hotkeys' in definition, `${row.id} 应是组合键项`).toBe(true)
      if (!('hotkeys' in definition)) continue

      expect(definition.hotkeys, row.id).toEqual(row.hotkeys)
      expect(definition.displayHotkey ?? null, row.id).toBe(row.displayHotkey)
      expect(getWorkflowCanvasShortcutDisplayKey(key), row.id).toBe(row.displayKey)
    }
  })

  it('AEGIS 新增项只多出剪切 / 保存 / 帮助 / 定位', () => {
    const added = shortcutsFixture.aegisPlan.added.map((row) => row.id)
    expect(added).toEqual(['workflow.cut', 'workflow.save', 'workflow.help', 'workflow.locate'])
    for (const row of shortcutsFixture.aegisPlan.added) {
      const key = row.id as WorkflowCanvasShortcutKey
      const definition: WorkflowCanvasShortcutDefinition = WORKFLOW_CANVAS_SHORTCUTS[key]
      expect('hotkeys' in definition, `${row.id} 应是组合键项`).toBe(true)
      if (!('hotkeys' in definition)) continue
      expect(definition.hotkeys, row.id).toEqual(row.hotkeys)
    }
  })

  it('组合键不重复：同一按键序列不得绑两个动作', () => {
    const seen = new Map<string, string>()
    for (const key of allKeys) {
      const definition = WORKFLOW_CANVAS_SHORTCUTS[key]
      if (!('hotkeys' in definition)) continue
      for (const chord of definition.hotkeys) {
        const owner = seen.get(chord)
        expect(owner, `${chord} 被 ${owner} 与 ${key} 同时占用`).toBeUndefined()
        seen.set(chord, key)
      }
    }
  })

  it('每个动作都只出现在一个帮助分组里，且分组覆盖全表', () => {
    const grouped = WORKFLOW_CANVAS_SHORTCUT_GROUPS.flatMap((group) => [...group.ids])
    expect([...grouped].sort()).toEqual([...allKeys].sort())
    expect(new Set(grouped).size).toBe(grouped.length)
  })

  it('名称与说明是中文，不是照抄上游英文文案', () => {
    for (const key of allKeys) {
      const definition = WORKFLOW_CANVAS_SHORTCUTS[key]
      expect(definition.name, key).toMatch(/[\u4e00-\u9fa5]/)
      expect(definition.description, key).toMatch(/[\u4e00-\u9fa5]/)
    }
  })
})
