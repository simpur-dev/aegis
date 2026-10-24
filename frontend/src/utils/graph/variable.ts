// Ported from Dify web/app/components/workflow/utils/variable.ts
// (tag 1.17.1, commit 8387590ace4a094de812b7847fc6a4c3a27cd52b).
// Copyright (c) LangGenius, Inc. Licensed under the modified Apache License 2.0.
// Changes: adapted to AEGIS; see THIRD_PARTY_NOTICES.md.

/**
 * 变量引用语法：画布内部一律用选择器数组，落到配置文本时才包成 `{{#node.key#}}`。
 * 两端的边界行为由 `dify-1.17.1/variable.json` 锁定——上游的"内嵌引用也按点切"是怪但确定的行为，
 * 端口照抄，发布校验再负责挡掉非整串引用。
 */
export type ValueSelector = readonly string[]

/** Dify `isExceptionVariable` 不迁移：AEGIS 没有 Dify 的 error-handle 通道。 */
export function variableTransformer(value: ValueSelector): string
export function variableTransformer(value: string): string[]
export function variableTransformer(value: ValueSelector | string): string | string[] {
  if (typeof value === 'string') return value.replace(/^\{\{#|#\}\}$/g, '').split('.')
  return `{{#${value.join('.')}#}}`
}

export function normalizeWorkflowOutputName(name?: string): string {
  return name?.trim() ?? ''
}

const UNSAFE_KEYS = new Set(['__proto__', 'prototype', 'constructor'])

/**
 * 输出键进变量面板前的最后一道闸：只收标识符形状，且不是原型链上的名字。
 * 上次运行的输出是服务端/节点给的，不能拿它当"可信键"直接铺进对象或选择器。
 */
export function isSafeVariableKey(key: string): boolean {
  return /^[a-z][a-z0-9_]*$/.test(key) && !UNSAFE_KEYS.has(key)
}
