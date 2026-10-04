/**
 * 五个 HTTP 客户端对同一次后端拒绝要说同一句话（跨客户端一致性门禁）。
 *
 * 这个仓库里错误包装是**分五份**的：`api/client.ts`（六张页的读接口）、
 * `api/map.ts`、`api/workflow.ts`、`api/reports.ts`、`api/assistant.ts`——
 * 各自带隔离边界（并行开发时互不波及），代价就是同一类错误会长成五种样子。
 * 真机把接口打成 422 逐页扫下来时，前四份都在还原原因，只剩一张图还在打
 * `数据读取失败：Request failed with status code 422`。
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
import { createWorkflowClient } from './workflow'

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
function calls(): Array<{ name: string; run: (instance: AxiosInstance) => Promise<unknown> }> {
  return [
    { name: '通用读接口', run: (instance) => createApiClient(instance).health() },
    { name: '一张图', run: (instance) => createMapApiClient(instance).stations() },
    { name: '流程编排', run: (instance) => createWorkflowClient(instance).definitions() },
    { name: '人工上报', run: (instance) => createReportsApiClient(instance).submit(toReportBody({ reporter: '巡护员', region_code: '540121', note: '沟道泥位抬升 1.2 米' })) },
    { name: '智能助手', run: (instance) => createAssistantApiClient(instance).capabilities() },
  ]
}

async function messageOf(run: (instance: AxiosInstance) => Promise<unknown>, status: number, data: unknown): Promise<string> {
  return await run(failingInstance(status, data))
    .then(() => '')
    .catch((error: unknown) => (error instanceof Error ? error.message : String(error)))
}

describe('五份客户端的失败面一致', () => {
  const validationBody = {
    detail: [{ loc: ['query', 'limit'], msg: REASON, type: 'greater_than' }],
  }

  for (const { name, run } of calls()) {
    it(`${name}：422 的数组原因要翻出来，不剩英文原话`, async () => {
      const message = await messageOf(run, 422, validationBody)
      expect(message, name).toContain(REASON)
      expect(message, name).toContain('422')
      expect(message, name).not.toContain('[object Object]')
      expect(message, name).not.toContain('Request failed with status code')
    })

    it(`${name}：字符串 detail（后端自己给的那句）原样带出`, async () => {
      const message = await messageOf(run, 503, { detail: '依赖不可用：认知镜像未装配（降级中）' })
      expect(message, name).toContain('依赖不可用：认知镜像未装配（降级中）')
      expect(message, name).toContain('503')
    })

    it(`${name}：没有响应体时留住 axios 那句话，不编一个中文原因`, async () => {
      const message = await messageOf(run, 500, undefined)
      expect(message, name).toContain('Request failed with status code 500')
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
    const message = await messageOf(calls()[0].run, 502, page502)
    expect(message).toContain('接口调用失败（HTTP 502）')
    expect(message).toContain('502 Bad Gateway')
    expect(message).not.toContain('</html>')
    expect(message.length).toBeLessThanOrEqual(240)
  })
})
