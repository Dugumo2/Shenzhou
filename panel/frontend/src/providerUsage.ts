import { formatDate } from './display.ts'

export type ResourceUsageQuality = 'current' | 'stale' | 'error' | 'gap' | 'missing'
export interface ResourceUsageAlert { code: string; severity: string; message: string }
export interface ResourceUsageCycle { kind: string; starts_at: string | null; ends_at: string | null; next_reset_at: string | null }
export interface ResourceUsageMeter {
  id: string; label: string; scope: string
  source_kind: 'estimate' | 'official'; quality: ResourceUsageQuality
  quota_bytes: string | null; used_bytes: string | null; remaining_bytes: string | null
  upload_bytes: string | null; download_bytes: string | null
  observed_at: string | null; expires_at: string | null
  cycle: ResourceUsageCycle | null; expires_on: string | null
  alerts: ResourceUsageAlert[]; details: Record<string, unknown>
}
export interface ProviderUsage {
  source_key?: string
  schema_version: 2; generated_at: string | null; meters: ResourceUsageMeter[]
  error?: { code: string; message: string }
}
// 保留原类型名供调用方迁移；字段合同统一为schema 2，不接收旧两供应商结构。
export type ProviderMeter = ResourceUsageMeter

export function resourceBytes(value: unknown): bigint | null {
  return typeof value === 'string' && value.length <= 128 && /^(0|[1-9]\d*)$/.test(value) ? BigInt(value) : null
}
export function resourceBytesLabel(value: unknown, unknown = '暂无记录'): string {
  const bytes = resourceBytes(value)
  if (bytes === null) return unknown
  if (bytes > 0n && bytes < 10_000_000n) return '< 0.01 GB'
  const hundredths = bytes * 100n / 1_000_000_000n
  const fraction = (hundredths % 100n).toString().padStart(2, '0').replace(/0+$/, '')
  return (hundredths / 100n).toLocaleString('zh-CN') + (fraction ? '.' + fraction : '') + ' GB'
}
export function resourceTimestamp(value: string | null): number | null {
  if (!value || !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp) ? timestamp : null
}
export function resourceTimeLabel(value: string | null): string {
  return resourceTimestamp(value) === null ? '未提供' : formatDate(value)
}
export function resourceDateLabel(value: string | null): string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return '未提供'
  const parsed = new Date(value + 'T00:00:00Z')
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value ? value : '未提供'
}
export function providerQuality(meter: ResourceUsageMeter, now = Date.now()): ResourceUsageQuality {
  if (meter.quality !== 'current') return meter.quality
  const observed = resourceTimestamp(meter.observed_at), expires = resourceTimestamp(meter.expires_at)
  return observed === null || expires === null || expires <= observed || expires <= now ? 'stale' : 'current'
}
export function resourceAlerts(meter: ResourceUsageMeter, now = Date.now()): ResourceUsageAlert[] {
  const alerts = [...meter.alerts]
  const observed = resourceTimestamp(meter.observed_at), expires = resourceTimestamp(meter.expires_at)
  // 缺口/错误与样本过期可同时存在；不改写后端质量和金额，也不为从未观测的资源造过期事实。
  if (observed !== null && expires !== null && expires <= now && !alerts.some(alert => alert.code === 'source_stale')) {
    alerts.push({ code: 'source_stale', severity: 'warning', message: '更新延迟，当前显示上次记录。' })
  }
  return alerts
}
export function billingPercent(meter: ResourceUsageMeter): number | null {
  const used = resourceBytes(meter.used_bytes), quota = resourceBytes(meter.quota_bytes)
  if (used === null || quota === null || quota === 0n) return null
  const ratio = used * 10_000n / quota
  return Number(ratio > 10_000n ? 10_000n : ratio) / 100
}
export function transferShare(meter: ResourceUsageMeter): number | null {
  const up = resourceBytes(meter.upload_bytes), down = resourceBytes(meter.download_bytes)
  if (up === null || down === null || up + down === 0n) return null
  return Number(up * 10_000n / (up + down)) / 100
}
// 旧测试导出名保持可导入；实现已不区分住宅或供应商。
export const residentialShare = transferShare

export function resourceScopeLabel(scope: string): string {
  return ({ machine: '整机流量', server: '服务器流量', external_node: '外部节点',
    node: '节点流量', route: '线路资源', service: '服务用量' } as Record<string, string>)[scope] || '资源统计'
}
const detailLabels: Record<string, string> = {
  accepted_historical_gap: '已接受迁入前缺口', coverage_started_at: '记录覆盖起点',
  estimate_start: '开始记录时间', baseline_bytes: '周期基线', plan_revision: '计划版本',
  quota_revision: '额度版本', source_revision: '来源版本', cycle_revision: '周期版本',
  lifetime_bytes: '历史累计字节', collection_gaps: '采集缺口次数', last_query_ok: '最近查询成功',
}
export function resourceDetailRows(details: Record<string, unknown>): { label: string; value: string }[] {
  return Object.entries(details || {}).map(([key, value]) => ({
    label: detailLabels[key] || key,
    value: value === null ? '未提供' : typeof value === 'boolean' ? (value ? '是' : '否')
      : typeof value === 'object' ? JSON.stringify(value) : String(value),
  }))
}
