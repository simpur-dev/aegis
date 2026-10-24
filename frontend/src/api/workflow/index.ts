/**
 * 工作流传输层公共出口：gateway 契约、默认实例与编解码工具。
 *
 * 组件与 store 只从这里取类型/默认 gateway；具体实现分在 contract/codec/client。
 */
import { createDefaultWorkflowClient } from './client'

export * from './contract'
export * from './codec'

export const workflowApi = createDefaultWorkflowClient()

export default workflowApi
