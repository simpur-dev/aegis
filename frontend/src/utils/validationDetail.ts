/**
 * 后端校验错误的统一还原处（三个出口共用：reports / workflow / assistant）。
 *
 * 后端在这里有三种形状，都得读成人话：
 * - `AegisError` / `WorkflowValidationError` → 400/503 的**字符串** detail；
 * - FastAPI 参数校验 → 422 的**数组** `[{loc, msg, type}]`；
 * - 503 的装配事实 → `{detail:{code,message}}` 这种再套一层的对象。
 *
 * 曾经每个页面各写一份拼装逻辑，结果：
 * ① 上报表单把数组 `String()` 出来，界面上是 `HTTP 422 [object Object]`；
 * ② 助手页遇到 422 只说"对话请求失败（HTTP 422）"，把"哪一格、为什么"整段丢了；
 * ③ 同一类错误在不同页面上长成三种样子，读的人学不会任何一种。
 *
 * 规则原文照抄（那是后端口径，翻译一次就多一个可能说错的地方），
 * 但字段名允许带中文标签——标签由调用方给（各页面自己那套叫法）。
 */

export type FieldLabels = Readonly<Record<string, string>>

function nameSegment(raw: string, labels: FieldLabels): string {
  const label = labels[raw]
  return label === undefined ? raw : `${label}（${raw}）`
}

/** 一条 FastAPI 校验错误 → `险情描述（note）：String should have at least 4 characters`。 */
function describeEntry(entry: unknown, labels: FieldLabels): string {
  if (typeof entry === 'string') return entry
  if (entry === null || typeof entry !== 'object') return String(entry ?? '')
  const record = entry as Record<string, unknown>
  const message = typeof record.msg === 'string' ? record.msg : ''
  const segments = Array.isArray(record.loc) ? record.loc.map(String).filter((seg) => seg !== 'body' && seg !== 'query') : []
  if (segments.length === 0) return message
  const head = segments[0] as string
  const rest = segments.slice(1).join('.')
  // 只有单层字段名时才配中文标签；`nodes.0.config.limit` 这种路径保持原形（下标正是要看的信息）
  const named = rest === '' ? nameSegment(head, labels) : `${head}.${rest}`
  return message === '' ? named : `${named}：${message}`
}

/**
 * 把响应的 detail 还原成一行中文界面能用的话；认不出形状时返回空串，
 * 由调用方回落到自己的兜底文案（这里不编造原因）。
 */
export function describeValidationDetail(raw: unknown, labels: FieldLabels = {}): string {
  if (typeof raw === 'string') return raw
  if (typeof raw === 'number' || typeof raw === 'boolean') return String(raw)
  if (Array.isArray(raw)) {
    return raw
      .map((entry) => describeEntry(entry, labels))
      .filter((text) => text !== '')
      .join('；')
  }
  if (raw !== null && typeof raw === 'object') {
    const record = raw as Record<string, unknown>
    if ('detail' in record) return describeValidationDetail(record.detail, labels)
    if (typeof record.message === 'string') return record.message
    return Object.entries(record)
      .map(([key, value]) => `${key}=${describeValidationDetail(value, labels)}`)
      .join(' ')
  }
  return ''
}
