import axios, { AxiosError, type AxiosRequestConfig } from 'axios'
import { describe, expect, it } from 'vitest'

import { createWorkflowClient } from '@/api/workflow/client'
import { decodeWorkflowDocument } from '@/api/workflow/codec'
import { WorkflowError } from '@/api/workflow/contract'

const WF = 'wf_0123456789ab'
const WF2 = 'wf_0123456789cd'

interface Recorded {
  method?: string
  url?: string
  body: unknown
  params: Record<string, unknown>
}

function clientWith(handler: (config: AxiosRequestConfig) => { status: number; data: unknown } | Promise<never>) {
  const seen: Recorded[] = []
  const instance = axios.create({
    baseURL: '/',
    adapter: async (config) => {
      seen.push({
        method: config.method,
        url: config.url,
        body: typeof config.data === 'string' ? JSON.parse(config.data) : config.data,
        params: (config.params ?? {}) as Record<string, unknown>,
      })
      const result = await handler(config)
      if (result.status >= 400) {
        throw new AxiosError(
          'request failed',
          'ERR_BAD_REQUEST',
          config,
          {},
          {
            status: result.status,
            statusText: 'error',
            headers: {},
            data: result.data,
            config,
          },
        )
      }
      return { data: result.data, status: result.status, statusText: 'OK', headers: {}, config }
    },
  })
  return { client: createWorkflowClient(instance), seen }
}

function wireNode(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    node_id: 'fetch_1',
    type: 'data_fetch',
    name: '数据接入',
    config: { region_code: 'RG-01' },
    sla_ms: 5_000,
    timeout_ms: 5_000,
    on_failure: 'retry',
    retry: { max_attempts: 1, backoff_ms: 200 },
    ...overrides,
  }
}

function wireDocument(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: 1,
    lineage_id: WF,
    name: '链路',
    description: '',
    nodes: [
      wireNode({ node_id: 'start', type: 'start', config: { variables: [] } }),
      wireNode({ node_id: 'end', type: 'end', config: {} }),
    ],
    edges: [{ source: 'start', target: 'end', condition: '' }],
    layout: { positions: { start: { x: 0, y: 0 }, end: { x: 280, y: 0 } }, viewport: null, notes: [] },
    ...overrides,
  }
}

function wireDraft(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    lineage_id: WF,
    base_workflow_id: null,
    revision: 1,
    document: wireDocument(),
    updated_at: '2026-10-22T00:00:00Z',
    ...overrides,
  }
}

function wireVersion(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    workflow_id: WF,
    lineage_id: WF,
    version: 1,
    name: '链路',
    description: '',
    status: 'active',
    node_count: 2,
    edge_count: 1,
    created_at: '2026-10-22T00:00:00Z',
    content_hash: 'sha256:abc',
    is_current: true,
    ...overrides,
  }
}

function wireInstance(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    instance_id: 'wfi_1',
    workflow_id: WF,
    workflow_version: 1,
    trace_id: 'trc_1',
    status: 'running',
    error: null,
    payload: {},
    nodes: [],
    ...overrides,
  }
}

describe('workflow gateway：旧端点', () => {
  it('节点面板 / 定义列表 / 单份定义都经 codec', async () => {
    const { client, seen } = clientWith((config) => {
      if (config.url?.endsWith('/node-types')) {
        return {
          status: 200,
          data: {
            count: 1,
            items: [
              {
                type: 'notify',
                description: '通报',
                category: 'io',
                required_config: ['text'],
                optional_config: [],
                min_config: {},
                outputs: ['notified'],
              },
            ],
          },
        }
      }
      if (config.url?.endsWith('/definitions')) {
        return {
          status: 200,
          data: {
            count: 1,
            items: [
              {
                workflow_id: WF,
                lineage_id: WF,
                name: '链路',
                version: 1,
                status: 'active',
                description: '',
                node_count: 2,
                edge_count: 1,
                created_at: '2026-10-22T00:00:00Z',
                content_hash: 'sha256:abc',
              },
            ],
          },
        }
      }
      return {
        status: 200,
        data: {
          workflow_id: WF,
          lineage_id: WF,
          schema_version: 1,
          name: '链路',
          description: '',
          version: 1,
          nodes: [wireNode()],
          edges: [],
          created_by: 'platform.workflow',
          created_at: '2026-10-22T00:00:00Z',
          status: 'active',
          content_hash: 'sha256:abc',
        },
      }
    })
    expect((await client.nodeTypes()).items[0]?.type).toBe('notify')
    expect((await client.definitions()).items[0]?.lineage_id).toBe(WF)
    expect((await client.definition(WF)).nodes[0]?.node_id).toBe('fetch_1')
    expect(seen.map((row) => row.url)).toEqual([
      '/api/v1/workflow/node-types',
      '/api/v1/workflow/definitions',
      `/api/v1/workflow/definitions/${WF}`,
    ])
  })

  it('创建 / 修订 / 归档 / 恢复的路径与载荷', async () => {
    const { client, seen } = clientWith(() => ({
      status: 200,
      data: { workflow_id: WF, name: '链路', version: 2, status: 'active' },
    }))
    const nodeInput = {
      node_id: 'fetch_1',
      type: 'data_fetch',
      name: ' 数据接入 ',
      config: { region_code: 'RG-01' },
      sla_ms: 5_000,
      timeout_ms: 5_000,
      on_failure: 'retry' as const,
      retry: { max_attempts: 1, backoff_ms: 200 },
    }
    await client.createDefinition({ name: ' 链路 ', description: ' 说明 ', nodes: [nodeInput], edges: [] })
    await client.reviseDefinition(WF, { description: ' 新说明 ' })
    await client.archiveDefinition(WF)
    await client.restoreDefinition(WF)
    expect(seen[0]?.body).toMatchObject({ name: '链路', description: '说明' })
    expect(seen[1]?.body).toEqual({ description: '新说明' })
    expect(seen[2]?.url).toBe(`/api/v1/workflow/definitions/${WF}/archive`)
    expect(seen[3]?.url).toBe(`/api/v1/workflow/definitions/${WF}/restore`)
  })

  it('实例控制：决策 / 改参 / 插入 / 旁路 / 中止', async () => {
    const { client, seen } = clientWith((config) => {
      if (config.url?.endsWith('/nodes') && config.method === 'post') {
        return { status: 200, data: { instance_id: 'wfi_1', node_id: 'notify_1', after: 'review_1' } }
      }
      if (config.url?.endsWith('/decision')) {
        return { status: 200, data: wireInstance({ status: 'succeeded' }) }
      }
      if (config.method === 'patch') {
        return { status: 200, data: { node_id: 'review_1', config: { text: '改' } } }
      }
      return { status: 200, data: wireInstance() }
    })
    await client.submitDecision('wfi_1', 'review_1', { choice: 'approve', comment: '同意', level: 2 })
    await client.patchNodeConfig('wfi_1', 'review_1', { text: '改' })
    await client.insertNode('wfi_1', {
      after: 'review_1',
      node: {
        node_id: 'notify_1',
        type: 'notify',
        name: '通报',
        config: { text: '注意' },
        sla_ms: 5_000,
        timeout_ms: 5_000,
        on_failure: 'retry',
        retry: { max_attempts: 1, backoff_ms: 200 },
      },
    })
    await client.bypassNode('wfi_1', 'review_1', '值班员决定')
    await client.abortInstance('wfi_1', '误启动')
    expect(seen[0]?.body).toEqual({ choice: 'approve', comment: '同意', level: 2 })
    expect(seen[1]?.body).toEqual({ config: { text: '改' } })
    expect(seen[2]?.body).toMatchObject({ after: 'review_1', node: { node_id: 'notify_1' } })
    expect(seen[3]?.params).toEqual({ reason: '值班员决定' })
    expect(seen[4]?.params).toEqual({ reason: '误启动' })
  })
})

describe('workflow gateway：试运行与实例图', () => {
  it('试运行：POST /previews 带上画布文档，GET /previews/{id} 取回同一份只读结果', async () => {
    const { client, seen } = clientWith((config) =>
      config.method === 'post'
        ? {
            status: 201,
            data: wireInstance({
              instance_id: 'pvp_1',
              preview: true,
              preview_id: 'pvp_1',
              status: 'succeeded',
              graph: { nodes: [wireNode()], edges: [], positions: { fetch_1: { x: 0, y: 0 } } },
            }),
          }
        : { status: 200, data: wireInstance({ instance_id: 'pvp_1', preview: true, preview_id: 'pvp_1' }) },
    )

    const created = await client.createPreview({
      document: decodeWorkflowDocument(wireDocument()),
      payload: { alert: '沟道泥位抬升' },
    })
    const fetched = await client.getPreview('pvp_1')

    expect(seen[0]?.method).toBe('post')
    expect(seen[0]?.url).toBe('/api/v1/workflow/previews')
    expect(seen[0]?.body).toMatchObject({
      document: { lineage_id: WF, name: '链路' },
      payload: { alert: '沟道泥位抬升' },
    })
    expect(seen[0]?.body).not.toHaveProperty('lineage_id')
    expect(seen[1]?.method).toBe('get')
    expect(seen[1]?.url).toBe('/api/v1/workflow/previews/pvp_1')
    expect(created.preview).toBe(true)
    expect(created.graph.nodes[0]?.node_id).toBe('fetch_1')
    expect(fetched.preview_id).toBe('pvp_1')
  })

  it('试运行也接受 lineage_id：服务端自己取草稿', async () => {
    const { client, seen } = clientWith(() => ({
      status: 201,
      data: wireInstance({ instance_id: 'pvp_1', preview: true, preview_id: 'pvp_1' }),
    }))
    await client.createPreview({ lineage_id: WF })
    expect(seen[0]?.body).toEqual({ lineage_id: WF })
  })

  it('插入节点带落点时原样送出', async () => {
    const { client, seen } = clientWith(() => ({
      status: 200,
      data: { instance_id: 'wfi_1', node_id: 'notify_1', after: 'review_1' },
    }))
    await client.insertNode('wfi_1', {
      after: 'review_1',
      node: {
        node_id: 'notify_1',
        type: 'notify',
        name: '通报',
        config: { text: '注意' },
        sla_ms: 5_000,
        timeout_ms: 5_000,
        on_failure: 'retry',
        retry: { max_attempts: 1, backoff_ms: 200 },
      },
      position: { x: 300, y: 40 },
    })
    expect(seen[0]?.body).toMatchObject({ after: 'review_1', position: { x: 300, y: 40 } })
  })
})

describe('workflow gateway：lineage 生命周期', () => {
  it('新建草稿 / 读草稿 / 存草稿 / 发布 / 恢复 / 改名', async () => {
    const { client, seen } = clientWith((config) => {
      if (config.url?.endsWith('/versions')) return { status: 200, data: { count: 1, items: [wireVersion()] } }
      if (config.url?.includes('/versions/')) return { status: 200, data: wireDocument() }
      if (config.url?.endsWith('/publish')) return { status: 200, data: wireVersion() }
      if (config.url?.endsWith('/revert-to-draft'))
        return { status: 200, data: wireDraft({ revision: 2, base_workflow_id: WF }) }
      if (config.method === 'patch')
        return { status: 200, data: { workflow_id: WF, lineage_id: WF, name: '新名', version: 1, draft: wireDraft() } }
      return { status: 200, data: wireDraft() }
    })
    expect((await client.createLineage(' 链路 ')).lineage_id).toBe(WF)
    expect((await client.getDraft(WF)).revision).toBe(1)
    const saved = await client.putDraft(WF, { base_revision: 1, document: decodeWorkflowDocument(wireDocument()) })
    expect(saved.revision).toBe(1)
    expect((await client.listVersions(WF))[0]?.is_current).toBe(true)
    expect((await client.getVersionDocument(WF)).name).toBe('链路')
    expect((await client.publish(WF, 2)).version).toBe(1)
    expect((await client.revertToDraft(WF, WF2)).base_workflow_id).toBe(WF)
    expect((await client.renameLineage(WF, ' 新名 ')).name).toBe('新名')
    expect(seen[0]?.body).toEqual({ name: '链路' })
    expect(seen[2]?.body).toMatchObject({ base_revision: 1 })
    expect(seen.find((row) => row.url?.endsWith('/publish'))?.body).toEqual({ base_revision: 2 })
    expect(seen.find((row) => row.url?.endsWith('/revert-to-draft'))?.body).toEqual({ workflow_id: WF2 })
    expect(seen.find((row) => row.method === 'patch')?.body).toEqual({ name: '新名' })
  })
})

describe('workflow gateway：typed error', () => {
  it('新统一错误体保留 code/details', async () => {
    const { client } = clientWith(() => ({
      status: 409,
      data: { detail: { code: 'revision_conflict', message: '草稿已被其他会话修改', details: { server_revision: 3 } } },
    }))
    const caught = await client.getDraft(WF).catch((error: unknown) => error)
    expect(caught).toBeInstanceOf(WorkflowError)
    expect((caught as WorkflowError).code).toBe('revision_conflict')
    expect((caught as WorkflowError).details.server_revision).toBe(3)
  })

  it('旧端点字符串 detail 也归一成 validation_failed', async () => {
    const { client } = clientWith(() => ({ status: 400, data: { detail: '工作流图存在环' } }))
    const caught = await client
      .createDefinition({ name: '链路', description: '', nodes: [], edges: [] })
      .catch((error: unknown) => error)
    expect(caught).toBeInstanceOf(WorkflowError)
    expect((caught as WorkflowError).code).toBe('validation_failed')
    expect((caught as WorkflowError).message).toBe('工作流图存在环')
  })

  it('响应格式不符时带路径', async () => {
    const { client } = clientWith(() => ({ status: 200, data: { count: 1, items: [{ type: 'notify' }] } }))
    const caught = await client.nodeTypes().catch((error: unknown) => error)
    expect(caught).toBeInstanceOf(WorkflowError)
    expect((caught as WorkflowError).code).toBe('invalid_response')
    expect((caught as WorkflowError).message).toContain('node-types.items[0].description')
  })

  it('网络失败归成 network_error', async () => {
    const { client } = clientWith(() => Promise.reject(new AxiosError('timeout of 20000ms exceeded', 'ECONNABORTED')))
    const caught = await client.instances().catch((error: unknown) => error)
    expect(caught).toBeInstanceOf(WorkflowError)
    expect((caught as WorkflowError).code).toBe('network_error')
    expect((caught as WorkflowError).message).toContain('请求超时')
  })
})
