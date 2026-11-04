/**
 * 会话流的分组模型：把一维帧序列切成"轮"，并给每轮分主帧 / 过程帧。
 *
 * 为什么单独一个 composable：页面与抽屉是同一个会话的两种容器，分组口径只能有一份。
 * 这也让分组规则可以脱离组件单测——它最容易出的错是"用户那句话贴错轮"，
 * 而这种错在界面上看起来只是"气泡跑到别处去了"，很难在 e2e 里定位。
 */

import { computed, type Ref } from 'vue'

import type { AssistantFrame } from '@/api/assistant'
import { QUIET_TYPES } from '@/utils/frameText'

/** 一轮对话：用户那句话（可能对不上号，是 null）+ 这一轮的帧。 */
export interface Turn {
  userText: string | null
  frames: AssistantFrame[]
}

export function useConversationModel(frames: Ref<AssistantFrame[]>, sentTexts: Ref<string[]>) {
  /**
   * 帧按轮分组：meta 帧是后端开新轮的标记；开头没跟 meta 的散帧（比如只回了 error）
   * 也自成一轮。用户的话按"从尾部数第 i 轮 ↔ sentTexts 倒数第 i 条"贴回去——
   * 两边都只在尾部追加、只在头部截断（MAX_PERSISTED_FRAMES），尾对齐在截断后依然成立。
   */
  const turns = computed<Turn[]>(() => {
    const result: Turn[] = []
    for (const frame of frames.value) {
      const last = result[result.length - 1]
      if (frame.type === 'meta' || last === undefined) result.push({ userText: null, frames: [frame] })
      else last.frames.push(frame)
    }
    for (let turnIdx = result.length - 1, sentIdx = sentTexts.value.length - 1; turnIdx >= 0 && sentIdx >= 0; turnIdx--, sentIdx--) {
      result[turnIdx].userText = sentTexts.value[sentIdx] ?? null
    }
    return result
  })

  /** 流式进行中报最新帧名：过程折叠了，但"此刻在等什么"仍要一眼可见。 */
  const lastFrame = computed(() => frames.value[frames.value.length - 1] ?? null)

  function mainFrames(turn: Turn): AssistantFrame[] {
    return turn.frames.filter((frame) => !QUIET_TYPES.has(frame.type))
  }

  function quietFrames(turn: Turn): AssistantFrame[] {
    return turn.frames.filter((frame) => QUIET_TYPES.has(frame.type))
  }

  /** 有 answer 才值得把过程收起来；只回了 error 的轮次，过程本身就是全部内容，原样摆出。 */
  function collapseQuiet(turn: Turn): boolean {
    return turn.frames.some((frame) => frame.type === 'answer')
  }

  return { turns, lastFrame, mainFrames, quietFrames, collapseQuiet }
}
