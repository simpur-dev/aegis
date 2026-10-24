import { describe, expect, it } from 'vitest'

import { ClipboardError, WORKFLOW_CLIPBOARD_KIND } from '@/utils/graph/clipboard'

import { readWorkflowClipboard, writeWorkflowClipboard, type ClipboardTextAdapter } from '../useWorkflowClipboard'

const payload = {
  nodes: [{ node: { node_id: 'a', type: 'start', config: {} }, position: { x: 1, y: 2 } }],
  edges: [{ source: 'a', target: 'b', condition: '' }],
}

describe('剪贴板适配层', () => {
  it('写入的文本能被自己的解析器读回', async () => {
    let written = ''
    const adapter: ClipboardTextAdapter = {
      readText: async () => written,
      writeText: async (text) => {
        written = text
      },
    }

    await writeWorkflowClipboard(payload, adapter)
    expect(written).toContain(WORKFLOW_CLIPBOARD_KIND)

    const parsed = await readWorkflowClipboard(adapter)
    expect(parsed.nodes).toHaveLength(1)
    expect(parsed.edges).toEqual([{ source: 'a', target: 'b', condition: '' }])
  })

  it('浏览器拒绝读取时给 read-failed，而不是静默空结果', async () => {
    const adapter: ClipboardTextAdapter = {
      readText: async () => {
        throw new DOMException('denied', 'NotAllowedError')
      },
      writeText: async () => {},
    }

    await expect(readWorkflowClipboard(adapter)).rejects.toMatchObject({
      code: 'read-failed',
      message: expect.stringContaining('denied'),
    })
  })

  it('浏览器拒绝写入时给 write-failed', async () => {
    const adapter: ClipboardTextAdapter = {
      readText: async () => '',
      writeText: async () => {
        throw new Error('Document is not focused')
      },
    }

    await expect(writeWorkflowClipboard(payload, adapter)).rejects.toMatchObject({ code: 'write-failed' })
  })

  it('环境没有 navigator.clipboard 时按操作方向报错', async () => {
    const original = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
    Object.defineProperty(globalThis, 'navigator', { value: {}, configurable: true })
    try {
      expect(globalThis.navigator.clipboard).toBeUndefined()
      await expect(readWorkflowClipboard()).rejects.toBeInstanceOf(ClipboardError)
      await expect(readWorkflowClipboard()).rejects.toMatchObject({ code: 'read-failed' })
      await expect(writeWorkflowClipboard(payload)).rejects.toMatchObject({ code: 'write-failed' })
    } finally {
      if (original !== undefined) Object.defineProperty(globalThis, 'navigator', original)
    }
  })

  it('默认适配器直接使用 navigator.clipboard', async () => {
    let written = ''
    const clipboard = {
      readText: async () => written,
      writeText: async (text: string) => {
        written = text
      },
    }
    const original = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
    Object.defineProperty(globalThis, 'navigator', { value: { clipboard }, configurable: true })
    try {
      await writeWorkflowClipboard(payload)
      const parsed = await readWorkflowClipboard()
      expect(parsed.nodes).toHaveLength(1)
    } finally {
      if (original !== undefined) Object.defineProperty(globalThis, 'navigator', original)
    }
  })

  it('读到非 AEGIS 片段时冒泡解析器的 code', async () => {
    const adapter: ClipboardTextAdapter = {
      readText: async () => '{"kind":"something-else","version":"1","nodes":[],"edges":[]}',
      writeText: async () => {},
    }
    await expect(readWorkflowClipboard(adapter)).rejects.toMatchObject({ code: 'wrong-kind' })
  })
})
