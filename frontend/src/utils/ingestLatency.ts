/**
 * 单条遥测读数的"接入时延"显示口径（零依赖，可离线单测）。
 *
 * 这里刻意**不重算第二套端到端时延**：平台真正量到的接入时延在时延账本里
 * （`ingest_end_to_end_seconds` 与 `ingest_publish_ms` / `ingest_store_ms` /
 * `ingest_store_to_query_ms` 那四条），指标页读的就是它。本函数只把单行上的
 * 两个时间戳摊开给人看，并且**在无从判断时说不判断**：
 *
 * 模拟站是"收到那一刻才编出观测值"，于是 `observed_at` 与 `ingested_at` 逐字节相同，
 * 相减恒为 0。原实现把这种情况显示成 `0 ms`——一个永远完美的数字，比空着更坏：
 * 现场会读成"接入零时延"，而它真正的意思是"这一行测不出接入时延"。
 */

export interface StampPair {
  observed_at?: string | null
  ingested_at?: string | null
}

export function ingestLatencyLabel(row: StampPair): string {
  const observed = row.observed_at
  const ingested = row.ingested_at
  if (!observed || !ingested) return '—（缺时间戳，无从判断）'
  if (observed === ingested) return '—（观测与入库同一时刻，测不出）'
  const ms = new Date(ingested).getTime() - new Date(observed).getTime()
  if (!Number.isFinite(ms)) return '—（时间戳不可解析）'
  // 站端时钟比平台慢就会出现负值：那是要被看见的时钟问题，不是"零时延"
  if (ms < 0) return `—（时钟倒挂 ${Math.abs(ms).toFixed(0)} ms）`
  return `${ms.toFixed(0)} ms`
}
