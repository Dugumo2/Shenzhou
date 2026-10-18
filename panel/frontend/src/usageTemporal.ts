import { usageNaturalRange } from './usageRangeCache.ts'
import type { UsageOverviewData } from './usage-types.ts'

/** 已跨过日界或账期的旧响应不得重新装回页面，即使网络刚刚成功。 */
export function usageResponseIsCurrent(value: UsageOverviewData, now = Date.now()): boolean {
  const range = usageNaturalRange('7d', now)
  const generated = Date.parse(value.generated_at)
  if (!Number.isFinite(generated) || generated < range.dayStart || generated >= range.dayEnd) return false
  return [value.summary.next_reset_at, value.current_cycle?.ends_at].every(boundary => boundary == null || Date.parse(boundary) > now)
}
