/**
 * SSE 切帧的单一实现。
 *
 * 为什么抽出来：助手对话（`api/assistant.ts`）与全站事件流（`composables/useEventStream.ts`）
 * 各自写了一份 `split('\n\n')` + 收集 `data:` 行，两处的 join 口径还不一样——助手用 `'\n'`，
 * 事件流用 `''`。单行 JSON 载荷看不出差别，出现多行 `data:` 时一个能解析、一个会拼坏。
 * 这里按 SSE 规范统一：块之间用空行切，多行 `data:` 用换行连接，注释帧不产出事件。
 */

export interface SseSplit {
  /** 完整的 `data:` 载荷（已去掉前缀）。注释帧不进这里。 */
  frames: string[]
  /** 还没收尾的最后一段，下次读流时接在前面。 */
  rest: string
}

/** 从缓冲区里切出完整帧；不完整的尾部原样还回去。 */
export function splitSseData(buffer: string): SseSplit {
  const parts = buffer.split(/\r?\n\r?\n/)
  const rest = parts.pop() ?? ''
  const frames: string[] = []
  for (const block of parts) {
    const data = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).replace(/^ /, ''))
    if (data.length === 0) continue // `: keep-alive` 这类注释帧没有任何 data 行
    frames.push(data.join('\n'))
  }
  return { frames, rest }
}

/** 流结束时把没有尾随空行的最后一帧也收掉（服务端可能不补 `\n\n`）。 */
export function drainSseData(buffer: string): string[] {
  const trimmed = buffer.trim()
  if (trimmed === '') return []
  return splitSseData(`${trimmed}\n\n`).frames
}
