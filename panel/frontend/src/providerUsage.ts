import { integerBytes, usagePercent } from './display.ts'

export interface ProviderMeter {
  quality: 'estimate' | 'provider_reported' | 'missing' | 'stale' | 'error'
  updated_at: string | null; expires_at: string | null; last_query_ok: boolean | null
  used_bytes: string | null; total_bytes: string | null; remaining_bytes: string | null
  upload_bytes?: string | null; download_bytes?: string | null
  estimate_start?: string | null; reset_at?: string | null; collection_gaps?: number | null
}
export interface ProviderUsage {
  state: 'disabled' | 'available' | 'missing' | 'error'
  snapshot_updated_at: string | null; residential: ProviderMeter; bwh: ProviderMeter
}
export function providerQuality(meter: ProviderMeter, now = Date.now()): ProviderMeter['quality'] {
  if (meter.quality === 'estimate' || meter.quality === 'provider_reported') {
    const expires = meter.expires_at ? Date.parse(meter.expires_at) : NaN
    if (!Number.isFinite(expires) || expires <= now) return 'stale'
  }
  return meter.quality
}
export function billingPercent(meter: ProviderMeter): number | null {
  const used = integerBytes(meter.used_bytes), total = integerBytes(meter.total_bytes)
  if (used === null || total === null || total === 0n) return null
  return usagePercent(meter.used_bytes, meter.total_bytes)
}
export function residentialShare(meter: ProviderMeter): number | null {
  const up = integerBytes(meter.upload_bytes ?? null), down = integerBytes(meter.download_bytes ?? null)
  if (up === null || down === null || up + down === 0n) return null
  return usagePercent(String(up), String(up + down))
}
