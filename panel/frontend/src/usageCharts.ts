import { integerBytes } from './display.ts'
import { reliableUsagePercent } from './usage-types.ts'
import type { UsageOverviewData, UsageDay } from './usage-types.ts'

export type ChartMetric = 'charged' | 'transfer'
export function boundedPercent(value: bigint, total: bigint): number {
  return total > 0n ? Number((value > total ? total : value) * 10_000n / total) / 100 : 0
}
export function quotaGauge(data: UsageOverviewData) {
  const used = integerBytes(data.summary.charged_bytes), total = integerBytes(data.summary.quota_bytes)
  const reliable = reliableUsagePercent(data)
  if (reliable !== null && used !== null && total !== null) return { mode: 'current', percent: boundedPercent(used, total) }
  // 过期样本只能画最后确认占比；灰色部分不得表示可用余额。
  if (data.quality.state === 'stale' && data.summary.quota_state === 'applied' && data.current_cycle
      && used !== null && total !== null && total > 0n) return { mode: 'recorded', percent: boundedPercent(used, total) }
  return { mode: 'unknown', percent: null }
}
export function transferShare(data: UsageOverviewData) {
  const up = integerBytes(data.summary.upload_bytes), down = integerBytes(data.summary.download_bytes)
  if (up === null || down === null) return null
  return { upPercent: boundedPercent(up, up + down), downPercent: boundedPercent(down, up + down), total: (up + down).toString(), empty: up + down === 0n }
}
function shanghaiDay(value: string | null) {
  if (!value || !Number.isFinite(Date.parse(value))) return null
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(value))
  const part = (type: string) => parts.find(item => item.type === type)?.value
  return `${part('year')}-${part('month')}-${part('day')}`
}
function dayEpoch(value: string) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const ms = Date.parse(value + 'T00:00:00Z')
  return Number.isFinite(ms) && new Date(ms).toISOString().slice(0, 10) === value ? ms : null
}
export function historyBars(data: UsageOverviewData, metric: ChartMetric) {
  const endDay = shanghaiDay(data.generated_at), startDay = shanghaiDay(data.history.range_start)
  if (!endDay || !startDay) return []
  const end = dayEpoch(endDay), rawStart = dayEpoch(startDay)
  if (end === null || rawStart === null) return []
  const explicitEnd = shanghaiDay(data.history.range_end)
  const last = explicitEnd ? Math.min(end, dayEpoch(explicitEnd)!) : end
  const first = Math.max(rawStart, last - 89 * 86_400_000)
  if (first > last) return []
  const byDate = new Map<string, UsageDay>()
  for (const row of data.history.days) if (dayEpoch(row.date) !== null) byDate.set(row.date, row)
  const points: { date: string; value: string | null; height: number; records: number }[] = []
  let max = 0n
  for (let day = first; day <= last; day += 86_400_000) {
    const date = new Date(day).toISOString().slice(0, 10), row = byDate.get(date)
    let value: bigint | null = null
    if (row && row.record_count > 0) {
      if (metric === 'charged') value = integerBytes(row.charged_bytes)
      else {
        const up = integerBytes(row.upload_bytes), down = integerBytes(row.download_bytes)
        if (up !== null && down !== null) value = up + down
      }
    }
    if (value !== null && value > max) max = value
    points.push({ date, value: value?.toString() ?? null, height: 0, records: row?.record_count ?? 0 })
  }
  // 缺失保留null；真实零保留0，绝不依据时间长度插值。
  return points.map(point => ({ ...point, height: point.value === null ? 0 : boundedPercent(BigInt(point.value), max) }))
}
