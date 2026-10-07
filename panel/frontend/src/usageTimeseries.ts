import { integerBytes } from './display.ts'
import { boundedPercent } from './usageCharts.ts'
import type { CalendarPeriod, UsageBucket, UsageOverviewData, UsageTimeseries } from './usage-types.ts'

export type TimeMetric = 'charged_bytes' | 'upload_bytes' | 'download_bytes'
export const timeMetricLabels: Record<TimeMetric, string> = { charged_bytes: '套餐消耗', upload_bytes: '原始上传', download_bytes: '原始下载' }

/** 只补日历轴，绝不把旧入账日期当流量发生日期，也不补零。 */
export function emptyCalendar(period: CalendarPeriod, asOf: string): UsageBucket[] {
  if (!Number.isFinite(Date.parse(asOf))) return []
  const local = new Date(Date.parse(asOf) + 8 * 3600_000)
  const y = local.getUTCFullYear(), m = local.getUTCMonth(), d = local.getUTCDate()
  const days = new Date(Date.UTC(y, m + 1, 0)).getUTCDate()
  const count = period === '7d' ? 7 : period === 'month' ? days : 12
  const current = Date.UTC(y, m, d)
  return Array.from({ length: count }, (_, index) => {
    const start = period === 'year' ? Date.UTC(y, index, 1) : Date.UTC(y, m, period === '7d' ? d - 6 + index : index + 1)
    const end = period === 'year' ? Date.UTC(y, index + 1, 1) : start + 86400_000
    const future = start > current
    return { start: new Date(start).toISOString().slice(0, 19) + '+08:00', end: new Date(end).toISOString().slice(0, 19) + '+08:00', state: future ? 'future' : 'missing', is_open: !future && end > current, covered_through: null, charged_bytes: null, upload_bytes: null, download_bytes: null }
  })
}

export function selectedTimeseries(data: UsageOverviewData | null, period: CalendarPeriod): UsageTimeseries | null {
  const series = data?.timeseries
  return series && series.period === period && series.service_id === data?.service_id ? series : null
}

export function timeBars(buckets: UsageBucket[], metric: TimeMetric) {
  const values = buckets.map(bucket => bucket.state === 'missing' || bucket.state === 'future' ? null : integerBytes(bucket[metric]))
  const maximum = values.reduce<bigint>((max, value) => value !== null && value > max ? value : max, 0n)
  return buckets.map((bucket, index) => ({ ...bucket, value: values[index]?.toString() ?? null, height: values[index] === null ? 0 : boundedPercent(values[index]!, maximum) }))
}
