/**
 * 五个 HTTP 客户端对同一次后端拒绝要说同一句话（跨客户端一致性门禁）。
 *
 * 这个仓库里错误包装是**分五份**的：`api/client.ts`（六张页的读接口）、
 * `api/map.ts`、`api/workflow/client.ts`、`api/reports.ts`、`api/assistant.ts`——
 * 各自带隔离边界（并行开发时互不波及），代价就是同一类错误会长成五种样子。
 * 真机把接口打成 422 逐页扫下来时，前四份都在还原原因，只剩一张图还在打
 * `数据读取失败：Request failed with status code 422`。
 *
 * 工作流那一份在 ADR-0010 之后改成了类型化 `WorkflowError`：原因照旧到界面上，
 * 但 HTTP 码改放进 `status` 字段，不再拼进 message，也不再漏 axios 的英文原话。
 *
 * 所以这条门禁不测某个页面偏好什么措辞，只钉三件底线：
 * ① 后端那句原文必须到界面上（不许只剩 HTTP 码）；
 * ② 不许漏 `[object Object]`；
 * ③ 不许把 axios 的英文原话当原因端上去。
 */

import type { AxiosInstance } from 'axios'
import axios, { AxiosError } from 'axios'
import { describe, expect, it } from 'vitest'

import { createApiClient } from './client'
import { createAssistantApiClient } from './assistant'
import { createMapApiClient } from './map'
import { createReportsApiClient, toReportBody } from './reports'
import { WorkflowError } from './workflow/contract'
import { createWorkflowClient } from './workflow/client'

const REASON = 'Value must be a positive integer'

function failingInstance(status: number, data: unknown): AxiosInstance {
  return axios.create({
    baseURL: '/',
    adapter: async (config) => {
      throw new AxiosError('Request failed with status code ' + String(status), 'ERR_BAD_REQUEST', config, {}, {
        status,
        statusText: 'error',
        headers: {},
        data,
        config,
      })
    },
  })
}

/** 五个客户端各挑一条真会走的出口：方法名不同，但都经过自己的 dispatch。 */
function calls(): Array<{ name: string; typed?: boolean; run: (instance: AxiosInstance) => Promise<unknown> }> {
  return [
    { name: '通用读接口', run: (instance) => createApiClient(instance).health() },
    { name: '一张图', run: (instance) => createMapApiClient(instance).stations() },
    { name: '流程编排', typed: true, run: (instance) => createWorkflowClient(instance).definitions() },
    { name: '人工上报', run: (instance) => createReportsApiClient(instance).submit(toReportBody({ reporter: '巡护员', region_code: '540121', note: '沟道泥位抬升 1.2 米' })) },
    { name: '智能助手', run: (instance) => createAssistantApiClient(instance).capabilities() },
  ]
}

interface Surface {
  message: string
  status: number | null
}

async function surfaceOf(run: (instance: AxiosInstance) => Promise<unknown>, status: number, data: unknown): Promise<Surface> {
  return await run(failingInstance(status, data))
    .then(() => ({ message: '', status: null }))
    .catch((error: unknown) => ({
      message: error instanceof Error ? error.message : String(error),
      status: error instanceof WorkflowError ? error.status : null,
    }))
}

describe('五份客户端的失败面一致', () => {
  const validationBody = {
    detail: [{ loc: ['query', 'limit'], msg: REASON, type: 'greater_than' }],
  }

  for (const { name, typed, run } of calls()) {
    it(`${name}：422 的数组原因要翻出来，不剩英文原话`, async () => {
      const surface = await surfaceOf(run, 422, validationBody)
      expect(surface.message, name).toContain(REASON)
      if (typed === true) {
        expect(surface.status, name).toBe(422)
      } else {
        expect(surface.message, name).toContain('422')
      }
      expect(surface.message, name).not.toContain('[object Object]')
      expect(surface.message, name).not.toContain('Request failed with status code')
    })

    it(`${name}：字符串 detail（后端自己给的那句）原样带出`, async () => {
      const surface = await surfaceOf(run, 503, { detail: '依赖不可用：认知镜像未装配（降级中）' })
      expect(surface.message, name).toContain('依赖不可用：认知镜像未装配（降级中）')
      if (typed === true) {
        expect(surface.status, name).toBe(503)
      } else {
        expect(surface.message, name).toContain('503')
      }
    })

    it(`${name}：没有响应体时留住 axios 那句话，不编一个中文原因`, async () => {
      const surface = await surfaceOf(run, 500, undefined)
      if (typed === true) {
        // 类型化传输层用中文码表兜底，但错误码仍在：axios 原话一律不进界面
        expect(surface.status, name).toBe(500)
        expect(surface.message, name).not.toContain('Request failed with status code')
        expect(surface.message.trim(), name).not.toBe('')
      } else {
        expect(surface.message, name).toContain('Request failed with status code 500')
      }
    })
  }

  /**
   * 上游网关（反代/LB）挂的时候，响应体常常是一整页 HTML。
   *
   * `detail` 是字符串会被原样还原成人能读的那行——这是特性（"Bad Gateway" 就该看得见），
   * 但一行提示撑到几千字就不是提示而是刷屏了，所以 `MAX_REASON_CHARS` 之外一律截断。
   */
  it('后端返回整页 HTML 时截一行就够，不把页面塞进提示', async () => {
    const page502 = `<html><head><title>502 Bad Gateway</title></head><body>${'网关诊断信息 '.repeat(60)}<hr></body></html>`
    const surface = await surfaceOf(calls()[0].run, 502, page502)
    expect(surface.message).toContain('接口调用失败（HTTP 502）')
    expect(surface.message).toContain('502 Bad Gateway')
    expect(surface.message).not.toContain('</html>')
    expect(surface.message.length).toBeLessThanOrEqual(240)
  })
})
