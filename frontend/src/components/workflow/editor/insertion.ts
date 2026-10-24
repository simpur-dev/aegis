/**
 * 块选择器的打开请求：工具栏、画布空白双击、边中点 “+” 共用同一份状态。
 *
 * 画布里的自定义边拿不到页面级 ref，所以这里用 provide/inject 把
 * “在某个屏幕坐标打开块选择器、可选地把新节点插进某条边”下发给整棵画布子树。
 */
import { inject, provide, type InjectionKey, type Ref } from 'vue'

export interface InsertionEdge {
  id: string
  source: string
  target: string
}

export interface InsertionRequest {
  /** 浮层锚点（视口坐标，position: fixed）。 */
  x: number
  y: number
  /** 从边中点打开时，选完块后把新节点插进这条边。 */
  edge?: InsertionEdge
  /** 落点语义：光标处（双击）或可视区中心（工具栏/空态）；省略按光标处理。 */
  placement?: 'cursor' | 'center'
}

export interface BlockInsertion {
  request: Ref<InsertionRequest | null>
  open: (request: InsertionRequest) => void
  close: () => void
}

export const INSERTION_KEY: InjectionKey<BlockInsertion> = Symbol('aegis.block-insertion')

export function provideBlockInsertion(insertion: BlockInsertion): BlockInsertion {
  provide(INSERTION_KEY, insertion)
  return insertion
}

/** 测试里可以只挂一棵带 provider 的子树；缺 provider 时返回 null，调用方自行决定降级。 */
export function useBlockInsertion(): BlockInsertion | null {
  return inject(INSERTION_KEY, null)
}
