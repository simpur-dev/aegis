/**
 * 工作流 HTTP gateway：全站**唯一**直接使用 axios 的工作流出口（ADR-0010）。
 *
 * 每个响应都先经 `codec` 从 `unknown` 解析；错误统一收成 `WorkflowError`，
 * 上层只认 `code/status/details`，不接触 axios 的英文消息。
 */
import axios, { type AxiosInstance, type AxiosRequestConfig } from 'axios'

import { installServiceToken } from '@/api/serviceToken'
import type { WorkflowGateway } from './contract'
import {
  WorkflowError,
  type DecisionInput,
  type DefinitionInput,
  type DraftPutInput,
  type InsertNodeInput,
  type ReviseInput,
  type StartInput,
} from './contract'
import {
  decodeDecisionResult,
  decodeDefinitionStatusResult,
  decodeDefinitionWriteResult,
  decodeDefinitionsResponse,
  decodeInsertNodeResult,
  decodeInstanceDetail,
  decodeInstancesResponse,
  decodeNodeConfigResult,
  decodeNodeTypesResponse,
  decodeRenameLineageResult,
  decodeVersionList,
  decodeVersionSummary,
  decodeWorkflowDef,
  decodeWorkflowDraft,
  decodeWorkflowDocument,
  decodeWorkflowError,
  encodeDraftPut,
} from './codec'

const BASE = '/api/v1/workflow'

function normalizeError(error: unknown): WorkflowError {
  if (error instanceof WorkflowError) return error
  if (axios.isCancel(error)) return new WorkflowError('aborted', 0, '请求已取消')
  if (axios.isAxiosError(error)) {
    const status = error.response?.status ?? 0
    if (status === 0) {
      const reason = error.code === 'ECONNABORTED' ? '请求超时' : error.message
      return new WorkflowError('network_error', 0, `接口调用失败（网络）：${reason}`)
    }
    const decoded = decodeWorkflowError(error.response?.data, status)
    return new WorkflowError(decoded.code, status, decoded.message, decoded.details)
  }
  return new WorkflowError('network_error', 0, error instanceof Error ? error.message : '未知错误')
}

export function createWorkflowClient(instance: AxiosInstance): WorkflowGateway {
  async function request<T>(
    config: AxiosRequestConfig,
    decode: (value: unknown) => T,
    signal?: AbortSignal,
  ): Promise<T> {
    try {
      const response = await instance.request<unknown>({ ...config, signal })
      return decode(response.data)
    } catch (error) {
      throw normalizeError(error)
    }
  }

  return {
    nodeTypes: (signal) => request({ url: `${BASE}/node-types` }, decodeNodeTypesResponse, signal),

    definitions: (signal) => request({ url: `${BASE}/definitions` }, decodeDefinitionsResponse, signal),

    definition: (workflowId, signal) =>
      request({ url: `${BASE}/definitions/${encodeURIComponent(workflowId)}` }, decodeWorkflowDef, signal),

    createDefinition: (payload: DefinitionInput, signal) =>
      request(
        {
          url: `${BASE}/definitions`,
          method: 'POST',
          data: {
            name: payload.name.trim(),
            description: payload.description.trim(),
            nodes: payload.nodes,
            edges: payload.edges,
          },
        },
        decodeDefinitionWriteResult,
        signal,
      ),

    reviseDefinition: (workflowId: string, payload: ReviseInput, signal) =>
      request(
        {
          url: `${BASE}/definitions/${encodeURIComponent(workflowId)}/revise`,
          method: 'POST',
          data: {
            ...(payload.nodes === undefined ? {} : { nodes: payload.nodes }),
            ...(payload.edges === undefined ? {} : { edges: payload.edges }),
            ...(payload.description === undefined ? {} : { description: payload.description.trim() }),
          },
        },
        decodeDefinitionWriteResult,
        signal,
      ),

    archiveDefinition: (workflowId, signal) =>
      request(
        { url: `${BASE}/definitions/${encodeURIComponent(workflowId)}/archive`, method: 'POST' },
        decodeDefinitionStatusResult,
        signal,
      ),

    restoreDefinition: (workflowId, signal) =>
      request(
        { url: `${BASE}/definitions/${encodeURIComponent(workflowId)}/restore`, method: 'POST' },
        decodeDefinitionStatusResult,
        signal,
      ),

    startInstance: (payload: StartInput, signal) =>
      request({ url: `${BASE}/instances`, method: 'POST', data: payload }, decodeInstanceDetail, signal),

    instances: (signal) => request({ url: `${BASE}/instances` }, decodeInstancesResponse, signal),

    instance: (instanceId, signal) =>
      request({ url: `${BASE}/instances/${encodeURIComponent(instanceId)}` }, decodeInstanceDetail, signal),

    submitDecision: (instanceId: string, nodeId: string, payload: DecisionInput, signal) =>
      request(
        {
          url: `${BASE}/instances/${encodeURIComponent(instanceId)}/nodes/${encodeURIComponent(nodeId)}/decision`,
          method: 'POST',
          data: payload,
        },
        decodeDecisionResult,
        signal,
      ),

    patchNodeConfig: (instanceId: string, nodeId: string, config, signal) =>
      request(
        {
          url: `${BASE}/instances/${encodeURIComponent(instanceId)}/nodes/${encodeURIComponent(nodeId)}`,
          method: 'PATCH',
          data: { config },
        },
        decodeNodeConfigResult,
        signal,
      ),

    insertNode: (instanceId: string, payload: InsertNodeInput, signal) =>
      request(
        {
          url: `${BASE}/instances/${encodeURIComponent(instanceId)}/nodes`,
          method: 'POST',
          data: { after: payload.after, node: payload.node },
        },
        decodeInsertNodeResult,
        signal,
      ),

    bypassNode: (instanceId: string, nodeId: string, reason = '', signal) =>
      request(
        {
          url: `${BASE}/instances/${encodeURIComponent(instanceId)}/nodes/${encodeURIComponent(nodeId)}/bypass`,
          method: 'POST',
          params: { reason },
        },
        decodeInstanceDetail,
        signal,
      ),

    abortInstance: (instanceId: string, reason = '', signal) =>
      request(
        { url: `${BASE}/instances/${encodeURIComponent(instanceId)}/abort`, method: 'POST', params: { reason } },
        decodeInstanceDetail,
        signal,
      ),

    createLineage: (name: string, signal) =>
      request({ url: `${BASE}/lineages`, method: 'POST', data: { name: name.trim() } }, decodeWorkflowDraft, signal),

    listVersions: (lineageId, signal) =>
      request({ url: `${BASE}/lineages/${encodeURIComponent(lineageId)}/versions` }, decodeVersionList, signal),

    getVersionDocument: (workflowId, signal) =>
      request({ url: `${BASE}/versions/${encodeURIComponent(workflowId)}` }, decodeWorkflowDocument, signal),

    getDraft: (lineageId, signal) =>
      request({ url: `${BASE}/lineages/${encodeURIComponent(lineageId)}/draft` }, decodeWorkflowDraft, signal),

    putDraft: (lineageId: string, payload: DraftPutInput, signal) =>
      request(
        {
          url: `${BASE}/lineages/${encodeURIComponent(lineageId)}/draft`,
          method: 'PUT',
          data: encodeDraftPut(payload),
        },
        decodeWorkflowDraft,
        signal,
      ),

    publish: (lineageId: string, baseRevision: number, signal) =>
      request(
        {
          url: `${BASE}/lineages/${encodeURIComponent(lineageId)}/publish`,
          method: 'POST',
          data: { base_revision: baseRevision },
        },
        decodeVersionSummary,
        signal,
      ),

    revertToDraft: (lineageId: string, workflowId: string, signal) =>
      request(
        {
          url: `${BASE}/lineages/${encodeURIComponent(lineageId)}/revert-to-draft`,
          method: 'POST',
          data: { workflow_id: workflowId },
        },
        decodeWorkflowDraft,
        signal,
      ),

    renameLineage: (lineageId: string, name: string, signal) =>
      request(
        { url: `${BASE}/lineages/${encodeURIComponent(lineageId)}`, method: 'PATCH', data: { name: name.trim() } },
        decodeRenameLineageResult,
        signal,
      ),
  }
}

/** 生产默认实例：与旧工作流 API 相同的超时与凭据头。 */
export function createDefaultWorkflowClient(): WorkflowGateway {
  const http = axios.create({ baseURL: '/', timeout: 20_000 })
  installServiceToken(http)
  return createWorkflowClient(http)
}
