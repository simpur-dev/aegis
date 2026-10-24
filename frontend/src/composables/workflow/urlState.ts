/**
 * 画布深链：地址栏只放服务端认得的三种 ID。
 *
 * - `lineage`：打开草稿（可带 `workflow` 指明当前发布版）
 * - `workflow`：打开只读版本
 * - `instance`：直接落在一张工单上（同时带它的 `workflow`）
 *
 * 纯函数放在 composables 层：视图只负责把 store 的值递进来，节奏与优先级在这里可测。
 */

export interface WorkflowUrlMode {
  kind: 'draft' | 'version' | 'instance'
  workflowId?: string
}

export interface WorkflowUrlState {
  lineageId: string
  publishedWorkflowId: string | null
  mode: WorkflowUrlMode
  instance: { instanceId: string; workflowId: string } | null
}

export interface WorkflowDeepLink {
  lineage: string
  workflow: string
  instance: string
}

export interface WorkflowUrlSync {
  changed: boolean
  query: Record<string, string>
}

const OWNED_KEYS = ['lineage', 'workflow', 'instance'] as const
type OwnedKey = (typeof OWNED_KEYS)[number]

function isOwnedKey(key: string): key is OwnedKey {
  return (OWNED_KEYS as readonly string[]).includes(key)
}

function queryValue(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** 读深链：同名参数出现多次（数组）或为空时一律当作没给。 */
export function readWorkflowDeepLink(query: Record<string, unknown>): WorkflowDeepLink {
  return {
    lineage: queryValue(query.lineage),
    workflow: queryValue(query.workflow),
    instance: queryValue(query.instance),
  }
}

/**
 * 由当前画布状态推出地址栏该有的查询串。
 *
 * 版本模式刻意不写 `lineage`：F5 时 bootstrap 先看 lineage，
 * 两者同写会让"只读版本"重启成草稿。实例模式同理，只认 instance。
 */
export function desiredWorkflowQuery(state: WorkflowUrlState): Record<string, string> {
  const query: Record<string, string> = {}
  if (state.mode.kind === 'draft') {
    if (state.lineageId !== '') query.lineage = state.lineageId
    if (state.instance !== null) {
      query.instance = state.instance.instanceId
      query.workflow = state.instance.workflowId
    } else if (state.publishedWorkflowId !== null) {
      query.workflow = state.publishedWorkflowId
    }
    return query
  }
  if (state.mode.kind === 'version') {
    if (state.mode.workflowId !== undefined && state.mode.workflowId !== '') {
      query.workflow = state.mode.workflowId
    }
    return query
  }
  if (state.instance !== null) {
    query.instance = state.instance.instanceId
    query.workflow = state.instance.workflowId
  }
  return query
}

/**
 * 合并地址栏：三种 ID 归本页管，其余查询参数原样保留。
 *
 * "有没有变"必须拿当前地址里的值比：只跟 desired 比会永远认为有值，
 * 于是新建画布后旧的 lineage 留在地址栏，F5 又把它打开。
 */
export function nextWorkflowQuery(current: Record<string, unknown>, desired: Record<string, string>): WorkflowUrlSync {
  const preserved: Record<string, string> = {}
  for (const [key, value] of Object.entries(current)) {
    if (!isOwnedKey(key) && typeof value === 'string') preserved[key] = value
  }
  const unchanged = OWNED_KEYS.every((key) => queryValue(current[key]) === (desired[key] ?? ''))
  if (unchanged) return { changed: false, query: preserved }
  return { changed: true, query: { ...preserved, ...desired } }
}
