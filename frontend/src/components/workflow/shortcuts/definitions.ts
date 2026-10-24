// Ported from Dify web/app/components/workflow/shortcuts/definitions.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.

/**
 * 上游用 `@tanstack/react-hotkeys` 的 `Hotkey` 类型；AEGIS 不引入 React 运行时，
 * 这里只需要"按键描述串"（`Mod+Shift+Z` 这种形式），匹配逻辑在 C3 的调度器里。
 */
export type Hotkey = string
export type IndividualKey = string

export type WorkflowCanvasShortcutId =
  | 'workflow.delete'
  | 'workflow.copy'
  | 'workflow.cut'
  | 'workflow.paste'
  | 'workflow.duplicate'
  | 'workflow.undo'
  | 'workflow.redo'
  | 'workflow.pointer-mode'
  | 'workflow.hand-mode'
  | 'workflow.organize'
  | 'workflow.zoom-to-fit'
  | 'workflow.zoom-to-100'
  | 'workflow.zoom-to-50'
  | 'workflow.zoom-out'
  | 'workflow.zoom-in'
  | 'workflow.save'
  | 'workflow.help'
  | 'workflow.locate'

interface WorkflowCanvasShortcutBase {
  readonly id: WorkflowCanvasShortcutId
  /** 帮助面板里展示的中文名；id 与按键与上游保持一致，便于对拍。 */
  readonly name: string
  readonly description: string
}

export interface WorkflowCanvasHotkeyDefinition extends WorkflowCanvasShortcutBase {
  readonly hotkeys: readonly Hotkey[]
  readonly displayHotkey?: Hotkey
}

export interface WorkflowCanvasHoldKeyDefinition extends WorkflowCanvasShortcutBase {
  readonly holdKey: IndividualKey
}

export type WorkflowCanvasShortcutDefinition = WorkflowCanvasHotkeyDefinition | WorkflowCanvasHoldKeyDefinition

/**
 * 保留上游 14 条、显式新增 4 条（保存 / 帮助 / 定位 / 剪切）。
 * 去掉的三条：comment-mode（AEGIS 备注直接在画布上拖，不做模式）、download-import-log
 * （没有导入日志入口）、dim-other-nodes（按住 Shift 既多选又压暗，语义会打架）。
 */
export const WORKFLOW_CANVAS_SHORTCUTS = {
  'workflow.delete': {
    id: 'workflow.delete',
    hotkeys: ['Delete', 'Backspace'],
    displayHotkey: 'Delete',
    name: '删除所选',
    description: '删除选中的节点或连线',
  },
  'workflow.copy': {
    id: 'workflow.copy',
    hotkeys: ['Mod+C'],
    name: '复制',
    description: '复制选中的节点（含它们之间的连线）',
  },
  'workflow.cut': {
    id: 'workflow.cut',
    hotkeys: ['Mod+X'],
    name: '剪切',
    description: '复制选中节点并从画布移除',
  },
  'workflow.paste': {
    id: 'workflow.paste',
    hotkeys: ['Mod+V'],
    name: '粘贴',
    description: '把剪贴板里的节点粘贴到画布',
  },
  'workflow.duplicate': {
    id: 'workflow.duplicate',
    hotkeys: ['Mod+D'],
    name: '创建副本',
    description: '原地复制选中节点并错开落点',
  },
  'workflow.undo': {
    id: 'workflow.undo',
    hotkeys: ['Mod+Z'],
    name: '撤销',
    description: '撤销上一步画布修改',
  },
  'workflow.redo': {
    id: 'workflow.redo',
    hotkeys: ['Mod+Y', 'Mod+Shift+Z'],
    displayHotkey: 'Mod+Y',
    name: '重做',
    description: '重做下一步画布修改',
  },
  'workflow.pointer-mode': {
    id: 'workflow.pointer-mode',
    hotkeys: ['V'],
    name: '指针模式',
    description: '切换回选择与拖拽',
  },
  'workflow.hand-mode': {
    id: 'workflow.hand-mode',
    hotkeys: ['H'],
    name: '抓手模式',
    description: '切换为平移画布',
  },
  'workflow.organize': {
    id: 'workflow.organize',
    hotkeys: ['Mod+O'],
    name: '自动整理',
    description: '按拓扑层重新排布节点',
  },
  'workflow.zoom-to-fit': {
    id: 'workflow.zoom-to-fit',
    hotkeys: ['Mod+1'],
    name: '适配画布',
    description: '把整张图收进视野',
  },
  'workflow.zoom-to-100': {
    id: 'workflow.zoom-to-100',
    hotkeys: ['Shift+1'],
    name: '缩放到 100%',
    description: '把画布缩放到原始大小',
  },
  'workflow.zoom-to-50': {
    id: 'workflow.zoom-to-50',
    hotkeys: ['Shift+5'],
    name: '缩放到 50%',
    description: '把画布缩放到一半',
  },
  'workflow.zoom-out': {
    id: 'workflow.zoom-out',
    hotkeys: ['Mod+-'],
    name: '缩小',
    description: '缩小画布',
  },
  'workflow.zoom-in': {
    id: 'workflow.zoom-in',
    hotkeys: ['Mod+='],
    displayHotkey: 'Mod+=',
    name: '放大',
    description: '放大画布',
  },
  'workflow.save': {
    id: 'workflow.save',
    hotkeys: ['Mod+S'],
    name: '保存草稿',
    description: '立即保存当前草稿',
  },
  'workflow.help': {
    id: 'workflow.help',
    hotkeys: ['?'],
    name: '快捷键帮助',
    description: '打开快捷键一览',
  },
  'workflow.locate': {
    id: 'workflow.locate',
    hotkeys: ['Mod+F'],
    name: '定位节点',
    description: '按名称查找并聚焦节点',
  },
} as const satisfies Record<WorkflowCanvasShortcutId, WorkflowCanvasShortcutDefinition>

export type WorkflowCanvasShortcutKey = keyof typeof WORKFLOW_CANVAS_SHORTCUTS

function isHoldKeyDefinition(
  definition: WorkflowCanvasShortcutDefinition,
): definition is WorkflowCanvasHoldKeyDefinition {
  return 'holdKey' in definition
}

/**
 * 上游对 `hotkeys[0]` 直接取值；AEGIS 的联合类型已把 holdKey 项分清，
 * 这里不必再靠 `in` 探测，返回类型也不含 undefined。
 */
export function getWorkflowCanvasShortcutDisplayKey(id: WorkflowCanvasShortcutKey): Hotkey | IndividualKey {
  const shortcut: WorkflowCanvasShortcutDefinition = WORKFLOW_CANVAS_SHORTCUTS[id]
  if (isHoldKeyDefinition(shortcut)) return shortcut.holdKey
  return shortcut.displayHotkey ?? shortcut.hotkeys[0]
}

/** 帮助面板与 `?` 弹层的展示顺序：按动作分组，而不是对象键序。 */
export const WORKFLOW_CANVAS_SHORTCUT_GROUPS: readonly {
  readonly title: string
  readonly ids: readonly WorkflowCanvasShortcutKey[]
}[] = [
  {
    title: '编辑',
    ids: [
      'workflow.delete',
      'workflow.copy',
      'workflow.cut',
      'workflow.paste',
      'workflow.duplicate',
      'workflow.undo',
      'workflow.redo',
    ],
  },
  { title: '模式', ids: ['workflow.pointer-mode', 'workflow.hand-mode'] },
  {
    title: '视图',
    ids: [
      'workflow.organize',
      'workflow.zoom-to-fit',
      'workflow.zoom-to-100',
      'workflow.zoom-to-50',
      'workflow.zoom-in',
      'workflow.zoom-out',
    ],
  },
  { title: '其他', ids: ['workflow.save', 'workflow.locate', 'workflow.help'] },
]
