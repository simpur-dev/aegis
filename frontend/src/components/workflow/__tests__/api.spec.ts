import type { AxiosRequestConfig } from 'axios'
import axios, { AxiosError } from 'axios'
import { describe, expect, it } from 'vitest'

import {
  createWorkflowClient,
  describeDetail,
  isWorkflowValidationError,
  toNodeInput,
  toNodeInputs,
  WorkflowApiError,
} from '@/api/workflow'
import type { NodeDef } from '@/utils/graph'

/**
 * 与 src/api/client.spec.ts 同一手法：注入桩适配器，只断言"方法 + 路径 + 载荷"，
 * 不依赖真实后端，也不共享其它模块的 axios 实例。
 */
interface Recorded {
  method?: string
  url?: string
  body: unknown
  params: Record<string, unknown>
}

function clientWith(handler: (config: AxiosRequestConfig) => { status: number; data: unknown }) {
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
      const result = handler(config)
      if (result.status >= 400) {
        throw new AxiosError('request failed', 'ERR_BAD_REQUEST', config, {}, {
          status: result.status,
          statusText: 'error',
          headers: {},
          data: result.data,
          config,
        })
      }
      return { data: result.data, status: result.status, statusText: 'OK', headers: {}, config }
    },
  })
  return { client: createWorkflowClient(instance), seen }
}

function nodeDef(overrides: Partial<NodeDef> = {}): NodeDef {
  return {
    node_id: 'fetch_1',
    type: 'data_fetch',
    name: '数据接入',
    config: { region_code: 'RG-01' },
    sla_ms: 5_000,
    timeout_ms: 4_000,
    on_failure: 'retry',
    retry: { max_attempts: 2, backoff_ms: 300 },
    ...overrides,
  }
}

describe('api/workflow 请求形状', () => {
  it('节点面板与定义列表用 GET 且落在 workflow 前缀下', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: { count: 0, items: [] } }))
    await client.nodeTypes()
    await client.definitions()
    expect(seen[0]?.url).toBe('/api/v1/workflow/node-types')
    expect(seen[0]?.body).toBeUndefined()
    expect(seen[1]?.url).toBe('/api/v1/workflow/definitions')
    expect(seen[0]?.method).toBe('get')
    expect(seen[1]?.method).toBe('get')
  })

  it('创建定义走 POST，载荷带 retry（读写同形，打开→保存才不会丢字段）', async () => {
    const { client, seen } = clientWith(() => ({ status: 201, data: { workflow_id: 'wf_1', name: '链路', version: 1 } }))
    await client.createDefinition({
      name: '泥石流防控链路',
      description: '',
      nodes: toNodeInputs([nodeDef()]),
      edges: [{ source: 'fetch_1', target: 'review_1', condition: 'high' }],
    })
    expect(seen[0]?.method).toBe('post')
    expect(seen[0]?.url).toBe('/api/v1/workflow/definitions')
    const body = seen[0]?.body as { nodes: Record<string, unknown>[]; edges: unknown[] }
    expect(Object.keys(body.nodes[0] ?? {}).sort()).toEqual([
      'config',
      'name',
      'node_id',
      'on_failure',
      'retry',
      'sla_ms',
      'timeout_ms',
      'type',
    ])
    expect(body.edges).toEqual([{ source: 'fetch_1', target: 'review_1', condition: 'high' }])
  })

  it('toNodeInput 保留全部可写字段（含 retry）', () => {
    expect(toNodeInput(nodeDef())).toEqual({
      node_id: 'fetch_1',
      type: 'data_fetch',
      name: '数据接入',
      config: { region_code: 'RG-01' },
      sla_ms: 5_000,
      timeout_ms: 4_000,
      on_failure: 'retry',
      retry: { max_attempts: 2, backoff_ms: 300 },
    })
  })

  it('修订接口路径带 workflow_id，body 允许只改 description', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: { workflow_id: 'wf_2', name: '链路', version: 2 } }))
    await client.reviseDefinition('wf_1', { description: '新说明' })
    expect(seen[0]?.url).toBe('/api/v1/workflow/definitions/wf_1/revise')
    expect(seen[0]?.method).toBe('post')
    expect(seen[0]?.body).toEqual({ description: '新说明' })
  })

  it('归档走 POST 且无请求体', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: { workflow_id: 'wf_1', status: 'archived' } }))
    await client.archiveDefinition('wf_1')
    expect(seen[0]?.url).toBe('/api/v1/workflow/definitions/wf_1/archive')
    expect(seen[0]?.body).toBeUndefined()
  })

  it('启动实例用 POST /instances 并回传 trace_id', async () => {
    const { client, seen } = clientWith(() => ({
      status: 200,
      data: { trace_id: 't-1', instance_id: 'wfi_1', workflow_id: 'wf_1', workflow_version: 1, status: 'running', error: null, nodes: [] },
    }))
    const result = await client.startInstance({ workflow_id: 'wf_1', payload: { region_code: 'RG-01' } })
    expect(seen[0]?.url).toBe('/api/v1/workflow/instances')
    expect(seen[0]?.body).toEqual({ workflow_id: 'wf_1', payload: { region_code: 'RG-01' } })
    expect(result.trace_id).toBe('t-1')
  })

  it('实例列表与详情走 GET，路径参数被编码', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: { count: 0, items: [] } }))
    await client.instances()
    await client.instance('wfi/异常 id')
    expect(seen[0]?.url).toBe('/api/v1/workflow/instances')
    expect(seen[1]?.url).toBe('/api/v1/workflow/instances/wfi%2F%E5%BC%82%E5%B8%B8%20id')
  })

  it('人工核签 POST decision，choice/by/comment 进请求体', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: {} }))
    await client.submitDecision('wfi_1', 'review_1', { choice: 'approve', by: '值班指挥员', comment: '同意发布' })
    expect(seen[0]?.url).toBe('/api/v1/workflow/instances/wfi_1/nodes/review_1/decision')
    expect(seen[0]?.method).toBe('post')
    expect(seen[0]?.body).toEqual({ choice: 'approve', by: '值班指挥员', comment: '同意发布' })
  })

  it('运行中改参用 PATCH，配置包在 config 键内', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: { node_id: 'fetch_1', config: {} } }))
    await client.patchNodeConfig('wfi_1', 'fetch_1', { limit: 50 })
    expect(seen[0]?.method).toBe('patch')
    expect(seen[0]?.url).toBe('/api/v1/workflow/instances/wfi_1/nodes/fetch_1')
    expect(seen[0]?.body).toEqual({ config: { limit: 50 } })
  })

  it('插入节点 POST /nodes，after 与 node 同级', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: { instance_id: 'wfi_1', node_id: 'notify_1', after: 'fetch_1' } }))
    await client.insertNode('wfi_1', { after: 'fetch_1', node: toNodeInput(nodeDef({ node_id: 'notify_1', type: 'notify', name: '通报', config: { text: '注意' } })) })
    expect(seen[0]?.url).toBe('/api/v1/workflow/instances/wfi_1/nodes')
    const body = seen[0]?.body as { after: string; node: Record<string, unknown> }
    expect(body.after).toBe('fetch_1')
    expect((body.node ?? {}).retry).toEqual({ max_attempts: 2, backoff_ms: 300 })
  })

  it('旁路与中止的 reason 是查询参数而非请求体', async () => {
    const { client, seen } = clientWith(() => ({ status: 200, data: {} }))
    await client.bypassNode('wfi_1', 'review_1', '设备已现场处置')
    await client.abortInstance('wfi_1', '上游数据中断')
    expect(seen[0]?.url).toBe('/api/v1/workflow/instances/wfi_1/nodes/review_1/bypass')
    expect(seen[0]?.params).toEqual({ reason: '设备已现场处置' })
    expect(seen[0]?.body).toBeUndefined()
    expect(seen[1]?.url).toBe('/api/v1/workflow/instances/wfi_1/abort')
    expect(seen[1]?.params).toEqual({ reason: '上游数据中断' })
  })
})

describe('api/workflow 失败映射', () => {
  it('400 WorkflowValidationError 被包装为带 status 与 detail 的错误', async () => {
    const { client } = clientWith(() => ({ status: 400, data: { detail: '工作流图存在环: a → b → a' } }))
    const error = await client.createDefinition({ name: '链路', description: '', nodes: [], edges: [] }).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(WorkflowApiError)
    const wrapped = error as WorkflowApiError
    expect(wrapped.status).toBe(400)
    expect(wrapped.detail).toEqual({ detail: '工作流图存在环: a → b → a' })
    expect(isWorkflowValidationError(wrapped)).toBe(true)
  })

  it('404 与 422 可与 400 区分（上层据此决定提示口径）', async () => {
    const notFound = clientWith(() => ({ status: 404, data: { detail: '工作流实例不存在' } }))
    const unprocessable = clientWith(() => ({ status: 422, data: { detail: '需要提供 workflow_id 或 workflow_name' } }))
    const error404 = await notFound.client.instance('wfi_missing').catch((caught: unknown) => caught)
    const error422 = await unprocessable.client.startInstance({}).catch((caught: unknown) => caught)
    expect((error404 as WorkflowApiError).status).toBe(404)
    expect(isWorkflowValidationError(error404)).toBe(false)
    expect((error422 as WorkflowApiError).status).toBe(422)
    expect(isWorkflowValidationError(error422)).toBe(false)
  })

  it('网络不可达时 status 为 0，不静默吞掉', async () => {
    const instance = axios.create({
      adapter: async () => {
        throw new AxiosError('connect ECONNREFUSED', 'ERR_NETWORK')
      },
    })
    const error = await createWorkflowClient(instance).definitions().catch((caught: unknown) => caught)
    expect((error as WorkflowApiError).status).toBe(0)
  })
})

/**
 * 错误原因必须是人能读的一句话。
 *
 * 真机把接口打成 422 时，画布上飘出来的是 `HTTP 422 [object Object]`——
 * 而 422 恰恰是最需要告诉人"哪个字段不对"的那一次（FastAPI 的 detail 是数组）。
 */
describe('describeDetail：后端 detail 还原成人读的一句话', () => {
  it('400 的 WorkflowValidationError 是字符串，原样给出', () => {
    expect(describeDetail('工作流图存在环')).toBe('工作流图存在环')
  })

  it('422 的 FastAPI 数组带出字段位置与原因', () => {
    const text = describeDetail({
      detail: [
        { loc: ['body', 'nodes', 0, 'config', 'limit'], msg: 'Input should be a valid integer', type: 'int_parsing' },
        { loc: ['body', 'name'], msg: 'String should have at least 1 character', type: 'string_too_short' },
      ],
    })
    expect(text).toContain('nodes.0.config.limit：Input should be a valid integer')
    expect(text).toContain('name：String should have at least 1 character')
    expect(text).not.toContain('[object Object]')
  })

  it('任何形状都不许漏出 [object Object]', () => {
    const shapes: unknown[] = [
      undefined,
      null,
      42,
      ['a', 'b'],
      { foo: { bar: 1 } },
      { detail: { detail: '递归一层也要读得懂' } },
      [{ loc: ['q', 'x'], msg: '必填' }],
    ]
    for (const shape of shapes) {
      const text = describeDetail(shape)
      expect(text, JSON.stringify(shape)).not.toContain('[object Object]')
    }
  })

  it('没有 msg 的裸对象摊成 k=v，不留空串以外的怪东西', () => {
    expect(describeDetail({ region_code: 'bad', limit: 0 })).toBe('region_code=bad limit=0')
  })
})
