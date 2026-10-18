import type { CalendarPeriod, UsageOverviewData } from './usage-types.ts'

export interface UsageRangeCacheKey {
  /** 由会话层维护的全局不透明代际；不得传入用户名、令牌或 Cookie。 */
  identityGeneration: string
  serviceId: string
  period: CalendarPeriod
}

export interface UsageRangeCacheEntry {
  snapshot: UsageOverviewData
  /** 成功读取时刻，不是来源采样时刻。 */
  lastReadAt: string
  cachedAt: number
}

export interface UsageRangeCacheOptions {
  now?: () => number
  ttlMs?: number
  maxEntries?: number
}

const DAY = 86_400_000
const SHANGHAI_OFFSET = 8 * 3_600_000

/** 上海自然范围用于缓存键；接口 range_end 仍是原 as_of，不改写成日历末尾。 */
export function usageNaturalRange(period: CalendarPeriod, now: number) {
  if (!Number.isFinite(now)) throw new RangeError('缓存时刻必须有效。')
  const local = new Date(now + SHANGHAI_OFFSET)
  const year = local.getUTCFullYear(), month = local.getUTCMonth(), day = local.getUTCDate()
  const dayStart = Date.UTC(year, month, day) - SHANGHAI_OFFSET
  const start = period === '7d' ? dayStart - 6 * DAY
    : Date.UTC(year, period === 'year' ? 0 : month, 1) - SHANGHAI_OFFSET
  const end = period === '7d' ? dayStart + DAY
    : Date.UTC(year + (period === 'year' ? 1 : 0), period === 'year' ? 0 : month + 1, 1) - SHANGHAI_OFFSET
  return { start, end, dayStart, dayEnd: dayStart + DAY }
}

interface StoredEntry extends UsageRangeCacheEntry {
  owner: string
  serviceId: string
  expiresAt: number
  cycle: string
  generatedAt: number
}

/** 仅缓存已接受的成功响应；请求去重、取消、迟到隔离由原读取控制器负责。 */
export function createUsageRangeCache(options: UsageRangeCacheOptions = {}) {
  const now = options.now ?? Date.now
  // 这是保留旧结果的内存预算，不代表后端采样周期或新鲜度承诺。
  const ttlMs = options.ttlMs ?? 5 * 60_000
  const maxEntries = options.maxEntries ?? 24
  if (!Number.isFinite(ttlMs) || ttlMs <= 0) throw new RangeError('缓存有效期必须为正数。')
  if (!Number.isSafeInteger(maxEntries) || maxEntries < 1) throw new RangeError('缓存容量必须为正整数。')
  const entries = new Map<string, StoredEntry>()

  function cacheKey(key: UsageRangeCacheKey, at: number) {
    if (!key.identityGeneration || !key.serviceId || !['7d', 'month', 'year'].includes(key.period)) return null
    const range = usageNaturalRange(key.period, at)
    return JSON.stringify([key.identityGeneration, key.serviceId, key.period, range.start, range.end])
  }

  function prune(at: number) {
    for (const [key, entry] of entries) {
      // 本机时钟回拨同样要求重验，避免出现无限延长的缓存。
      if (at >= entry.expiresAt || at < entry.cachedAt) entries.delete(key)
    }
  }

  function clearService(identityGeneration: string, serviceId: string) {
    for (const [key, entry] of entries) {
      if (entry.owner === identityGeneration && entry.serviceId === serviceId) entries.delete(key)
    }
  }

  function clearOwner(identityGeneration: string) {
    for (const [key, entry] of entries) if (entry.owner === identityGeneration) entries.delete(key)
  }

  return {
    get(key: UsageRangeCacheKey): UsageRangeCacheEntry | null {
      const at = now()
      prune(at)
      const id = cacheKey(key, at)
      const entry = id === null ? undefined : entries.get(id)
      if (!entry) return null
      entries.delete(id!)
      entries.set(id!, entry)
      return { snapshot: entry.snapshot, lastReadAt: entry.lastReadAt, cachedAt: entry.cachedAt }
    },
    set(key: UsageRangeCacheKey, snapshot: UsageOverviewData, lastReadAt: string): boolean {
      const at = now()
      prune(at)
      const id = cacheKey(key, at)
      if (id === null) return false
      const range = usageNaturalRange(key.period, at)
      const generatedAt = Date.parse(snapshot.generated_at)
      const series = snapshot.timeseries
      // 只有本范围的实际响应可成为缓存；无缓存时返回 null，不构造 missing/零值图。
      if (snapshot.service_id !== key.serviceId || snapshot.period !== key.period || snapshot.time_zone !== 'Asia/Shanghai'
        || !Number.isFinite(Date.parse(lastReadAt)) || !Number.isFinite(generatedAt)
        || generatedAt < range.dayStart || generatedAt >= range.dayEnd
        || !series || series.service_id !== key.serviceId || series.period !== key.period || series.time_zone !== 'Asia/Shanghai'
        || Date.parse(series.range_start) !== range.start
        || !Number.isFinite(Date.parse(series.as_of))
        || Date.parse(series.as_of) < range.dayStart || Date.parse(series.as_of) >= range.dayEnd
        || Date.parse(series.range_end) !== Date.parse(series.as_of)) return false

      let expiresAt = Math.min(at + ttlMs, range.dayEnd)
      // 月/年键可能不变，但开放桶、未来桶和当期摘要在日界或重置点必须重验。
      for (const boundary of [snapshot.summary.next_reset_at, snapshot.current_cycle?.ends_at]) {
        if (boundary != null) {
          const end = Date.parse(boundary)
          if (!Number.isFinite(end) || end <= at) return false
          expiresAt = Math.min(expiresAt, end)
        }
      }
      const cycle = JSON.stringify([snapshot.current_cycle?.starts_at ?? null, snapshot.current_cycle?.ends_at ?? null, snapshot.summary.next_reset_at])
      const related = [...entries.values()].filter(entry => entry.owner === key.identityGeneration && entry.serviceId === key.serviceId)
      // 旧响应不能把已知新账期倒退；新账期响应使其他范围的旧摘要一起失效。
      if (related.some(entry => entry.generatedAt > generatedAt && entry.cycle !== cycle)) return false
      if (related.some(entry => entry.cycle !== cycle)) clearService(key.identityGeneration, key.serviceId)
      const previous = entries.get(id)
      if (previous && previous.generatedAt > generatedAt) return false
      entries.delete(id)
      entries.set(id, { snapshot, lastReadAt, cachedAt: at, expiresAt, cycle, generatedAt, owner: key.identityGeneration, serviceId: key.serviceId })
      while (entries.size > maxEntries) entries.delete(entries.keys().next().value!)
      return true
    },
    clearService,
    clearOwner,
    clear() { entries.clear() },
    get size() { prune(now()); return entries.size },
  }
}

/** 模块内存允许同一身份离页返回；退出/权限变化必须由会话层主动清理。 */
export const usageRangeCache = createUsageRangeCache()
