/**
 * 四个 store 的测试底板：一份全量 gateway 桩 + 常用文档/实例夹具。
 *
 * store 层测试不打网络：gateway 由调用方注入，桩只回这份文件里造的领域对象。
 */
import { vi } from 'vitest'
import type { MockedFunction } from 'vitest'

import type { InstanceDetail, InstanceGraph, InstanceNodeRun, WorkflowGateway } from '@/api/workflow/contract'
import type { EdgeDef, JsonValue, NodeDef, NodeType, NodeState, Position } from '@/utils/graph'
import type { WorkflowDocument, WorkflowDraft, WorkflowVersionSummary } from '@/utils/graph/document'

export type StubGateway = { [K in keyof WorkflowGateway]: MockedFunction<WorkflowGateway[K]> }

export const LINEAGE = 'wf_0123456789ab'
export const PUBLISHED = 'wf_0123456789cd'
export const OTHER_WORKFLOW = 'wf_0123456789ef'
export const INSTANCE = 'wfi_0123456789ab'
export const OTHER_INSTANCE = 'wfi_0123456789cd'
export const UPDATED_AT = '2026-10-22T00:00:00Z'

/** 每个方法都先立成 mock；用例只覆盖自己会走的那几条。 */
export function gatewayStub(overrides: Partial<Record<keyof WorkflowGateway, unknown>> = {}): StubGateway {
  const stub = {
    nodeTypes: vi.fn(),
    definitions: vi.fn(),
    definition: vi.fn(),
    createDefinition: vi.fn(),
    reviseDefinition: vi.fn(),
    archiveDefinition: vi.fn(),
    restoreDefinition: vi.fn(),
    startInstance: vi.fn(),
    instances: vi.fn(),
    instance: vi.fn(),
    submitDecision: vi.fn(),
    patchNodeConfig: vi.fn(),
    insertNode: vi.fn(),
    bypassNode: vi.fn(),
    abortInstance: vi.fn(),
    createLineage: vi.fn(),
    listVersions: vi.fn(),
    getVersionDocument: vi.fn(),
    getDraft: vi.fn(),
    putDraft: vi.fn(),
    publish: vi.fn(),
    revertToDraft: vi.fn(),
    renameLineage: vi.fn(),
  } as unknown as StubGateway
  return Object.assign(stub, overrides) as StubGateway
}

export function nodeDef(
  nodeId: string,
  type: NodeType = 'data_fetch',
  config: Record<string, JsonValue> = {},
): NodeDef {
  return {
    node_id: nodeId,
    type,
    name: nodeId,
    config,
    sla_ms: 5_000,
    timeout_ms: 5_000,
    on_failure: 'retry',
    retry: { max_attempts: 1, backoff_ms: 200 },
  }
}

export function workflowDocument(overrides: Partial<WorkflowDocument> = {}): WorkflowDocument {
  return {
    schema_version: 1,
    lineage_id: LINEAGE,
    name: '已存链路',
    description: '',
    nodes: [nodeDef('fetch_1'), nodeDef('review_1', 'human_review', { options: ['approve', 'reject'] })],
    edges: [{ source: 'fetch_1', target: 'review_1', condition: '' }],
    layout: {
      positions: { fetch_1: { x: 0, y: 0 }, review_1: { x: 280, y: 0 } },
      viewport: null,
      notes: [],
    },
    ...overrides,
  }
}

export function draftOf(
  document: WorkflowDocument = workflowDocument(),
  overrides: Partial<WorkflowDraft> = {},
): WorkflowDraft {
  return {
    lineage_id: document.lineage_id,
    base_workflow_id: null,
    revision: 1,
    document,
    updated_at: UPDATED_AT,
    ...overrides,
  }
}

export function versionSummary(overrides: Partial<WorkflowVersionSummary> = {}): WorkflowVersionSummary {
  return {
    workflow_id: PUBLISHED,
    lineage_id: LINEAGE,
    version: 1,
    name: '已存链路',
    description: '',
    status: 'active',
    node_count: 2,
    edge_count: 1,
    created_at: UPDATED_AT,
    content_hash: 'sha256:abc',
    is_current: true,
    ...overrides,
  }
}

export function nodeRun(nodeId: string, state: NodeState, overrides: Partial<InstanceNodeRun> = {}): InstanceNodeRun {
  return {
    node_id: nodeId,
    type: 'data_fetch',
    state,
    attempts: 1,
    schedule_latency_ms: 3,
    duration_ms: 12,
    output: {},
    error: null,
    notes: [],
    ...overrides,
  }
}

export function instanceGraph(
  nodes: NodeDef[] = [nodeDef('fetch_1'), nodeDef('review_1', 'human_review', { options: ['approve', 'reject'] })],
  edges: EdgeDef[] = [{ source: 'fetch_1', target: 'review_1', condition: '' }],
  positions: Record<string, Position> = {
    fetch_1: { x: 0, y: 0 },
    review_1: { x: 280, y: 0 },
  },
): InstanceGraph {
  return { nodes, edges, positions }
}

export function instanceDetail(overrides: Partial<InstanceDetail> = {}): InstanceDetail {
  return {
    instance_id: INSTANCE,
    workflow_id: PUBLISHED,
    workflow_version: 1,
    lineage_id: LINEAGE,
    trace_id: 'trace-1',
    status: 'running',
    error: null,
    preview: false,
    preview_id: null,
    created_at: UPDATED_AT,
    finished_at: null,
    payload: {},
    results: {},
    nodes: [],
    graph: instanceGraph(),
    ...overrides,
  }
}

export function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => {}
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

/** jsdom 的 visibilityState 是只读的；轮询用例临时改写，结束时还原。 */
export function setVisibility(state: DocumentVisibilityState): () => void {
  const descriptor = Object.getOwnPropertyDescriptor(document, 'visibilityState')
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: state })
  return () => {
    if (descriptor === undefined) delete (document as { visibilityState?: unknown }).visibilityState
    else Object.defineProperty(document, 'visibilityState', descriptor)
  }
}
