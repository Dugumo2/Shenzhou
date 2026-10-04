import { integerBytes, usagePercent } from './display.ts'
import type { ProviderUsage } from './providerUsage'

export type UsagePeriod = 'current' | '7d' | '30d'
export type UsageBytes = string | null
export interface UsageTotals { upload_bytes: UsageBytes; download_bytes: UsageBytes; charged_bytes: UsageBytes }
export interface UsageDay extends UsageTotals { date: string; record_count: number }
export interface UsageOverviewData {
  provider_usage?: ProviderUsage
  service_id: string; source_type: 'entitlement' | 'membership' | 'p8'; time_zone: 'Asia/Shanghai'
  generated_at: string; period: UsagePeriod
  current_cycle: { starts_at: string; ends_at: string } | null
  summary: UsageTotals & { quota_bytes: UsageBytes; quota_state: 'applied' | 'configured' | 'unknown'; remaining_bytes: UsageBytes; next_reset_at: string | null }
  quality: { state: 'measured' | 'stale' | 'gap' | 'unknown'; message: string; collected_at: string | null }
  history: { kind: 'confirmed_ledger_postings'; date_basis: 'created_at'; range_start: string | null; range_end: string | null
    totals: UsageTotals | null; record_count: number; excluded_record_count: number; days: UsageDay[]
    day_limit: number; days_truncated: boolean; message: string }
}

export function reliableUsagePercent(data: UsageOverviewData): number | null {
  if (data.quality.state !== 'measured' || data.summary.quota_state !== 'applied') return null
  const used = integerBytes(data.summary.charged_bytes), quota = integerBytes(data.summary.quota_bytes)
  const remaining = integerBytes(data.summary.remaining_bytes)
  if (used === null || quota === null || remaining === null || quota === 0n) return null
  if (remaining !== (quota > used ? quota - used : 0n)) return null
  return usagePercent(data.summary.charged_bytes, data.summary.quota_bytes)
}
