/**
 * 画布子树的依赖注入面：四个 store 由 `WorkflowView` 一次提供。
 *
 * 组件不直接 import 全局单例，测试可以注入同一形状的 fake；缺 provider 时立刻抛错，
 * 而不是悄悄退回某个全局 store 让用例测到另一套对象。
 */
import { inject, provide, type InjectionKey } from 'vue'

import { useCatalogStore, type CatalogStore } from '@/stores/workflow/catalog'
import { useDocumentStore, type DocumentStore } from '@/stores/workflow/document'
import { useEditorStore, type EditorStore } from '@/stores/workflow/editor'
import { useRunStore, type RunStore } from '@/stores/workflow/run'

export interface EditorContext {
  catalog: CatalogStore
  document: DocumentStore
  editor: EditorStore
  run: RunStore
}

export const EDITOR_CONTEXT_KEY: InjectionKey<EditorContext> = Symbol('aegis.editor-context')

export function createEditorContext(): EditorContext {
  return {
    catalog: useCatalogStore(),
    document: useDocumentStore(),
    editor: useEditorStore(),
    run: useRunStore(),
  }
}

export function provideEditorContext(context: EditorContext = createEditorContext()): EditorContext {
  provide(EDITOR_CONTEXT_KEY, context)
  return context
}

export function useEditorContext(): EditorContext {
  const context = inject(EDITOR_CONTEXT_KEY)
  if (context === undefined) {
    throw new Error('EditorContext 未提供：请在 WorkflowView 或测试的挂载根上调用 provideEditorContext()')
  }
  return context
}
