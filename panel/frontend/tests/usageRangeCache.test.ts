import test from 'node:test'
import assert from 'node:assert/strict'
import { createUsageRangeCache, usageNaturalRange } from '../src/usageRangeCache.ts'
import type { UsageRangeCacheKey } from '../src/usageRangeCache.ts'
import type { UsageOverviewData, CalendarPeriod } from '../src/usage-types.ts'

const INITIAL = Date.parse('2026-10-18T04:00:00Z')
const key = (period: CalendarPeriod = '7d', serviceId = 'canonical-a', identityGeneration = 'generation:1'): UsageRangeCacheKey => ({ period, serviceId, identityGeneration })
function snapshot(at = INITIAL, selected = key()): UsageOverviewData {
  const range = usageNaturalRange(selected.period, at)
  const stamp = new Date(at).toISOString()
  return {
    service_id: selected.serviceId, source_type: 'entitlement', time_zone: 'Asia/Shanghai', generated_at: stamp, period: selected.period,
    current_cycle: { starts_at: new Date(at - 10 * 86400_000).toISOString(), ends_at: new Date(at + 20 * 86400_000).toISOString() },
    summary: { quota_bytes: '1000000000000', quota_state: 'applied', charged_bytes: '98253546067', remaining_bytes: '901746453933', upload_bytes: null, download_bytes: null, next_reset_at: null },
    quality: { state: 'gap', message: '保留原质量', collected_at: new Date(at - 60_000).toISOString() },
    history: { kind: 'confirmed_ledger_postings', date_basis: 'created_at', range_start: null, range_end: null, totals: null, record_count: 0, excluded_record_count: 0, days: [], day_limit: 90, days_truncated: false, message: '' },
    timeseries: { service_id: selected.serviceId, summary_revision: 'revision:1', time_zone: 'Asia/Shanghai', period: selected.period,
      granularity: selected.period === 'year' ? 'month' : 'day', range_start: new Date(range.start).toISOString(), range_end: stamp, as_of: stamp,
      totals: { upload_bytes: null, download_bytes: null, charged_bytes: '20', state: 'partial', unallocated_charged_bytes: null },
      buckets: [{ start: new Date(range.start).toISOString(), end: new Date(range.start + 86400_000).toISOString(), state: 'missing', is_open: false, covered_through: null, upload_bytes: null, download_bytes: null, charged_bytes: null }],
      unallocated: [], unallocated_next_cursor: null, boundary_pending_count: 0, message_code: 'partial_coverage' },
  }
}

test('首次无缓存返回null，三范围往返及新读取实例共享缓存且不改原金额/质量/时刻', () => {
  const cache = createUsageRangeCache({ now: () => INITIAL })
  assert.equal(cache.get(key()), null)
  const snapshots = (['7d', 'month', 'year'] as const).map(period => snapshot(INITIAL, key(period)))
  const original = JSON.stringify(snapshots)
  for (const data of snapshots) assert.equal(cache.set(key(data.period as CalendarPeriod), data, '2026-10-18T04:00:01Z'), true)
  const readFromNewPage = () => cache.get(key())
  assert.equal(readFromNewPage()?.snapshot, snapshots[0])
  for (const data of snapshots) assert.equal(cache.get(key(data.period as CalendarPeriod))?.snapshot, data)
  assert.equal(cache.get(key())?.lastReadAt, '2026-10-18T04:00:01Z')
  assert.equal(JSON.stringify(snapshots), original)
})

test('身份代际、canonical服务与period严格隔离，未知键不借用其他图', () => {
  const cache = createUsageRangeCache({ now: () => INITIAL })
  cache.set(key(), snapshot(), new Date(INITIAL).toISOString())
  for (const other of [key('month'), key('7d', 'alias-a'), key('7d', 'canonical-a', 'generation:2')]) assert.equal(cache.get(other), null)
  assert.equal(cache.size, 1)
})

test('上海7天/月/年自然范围包含闰月、月末和年末', () => {
  const at = Date.parse('2028-02-29T16:00:00Z')
  assert.equal(new Date(usageNaturalRange('7d', at).start).toISOString(), '2028-02-23T16:00:00.000Z')
  assert.equal(new Date(usageNaturalRange('month', at).start).toISOString(), '2028-02-29T16:00:00.000Z')
  assert.equal(new Date(usageNaturalRange('month', at - 1).end).toISOString(), '2028-02-29T16:00:00.000Z')
  assert.equal(new Date(usageNaturalRange('year', at).end).toISOString(), '2028-12-31T16:00:00.000Z')
  assert.equal(new Date(usageNaturalRange('year', Date.parse('2026-12-31T16:00:00Z')).start).toISOString(), '2026-12-31T16:00:00.000Z')
})

test('TTL到期按原写入时间失效，访问不会续命', () => {
  let at = INITIAL
  const cache = createUsageRangeCache({ now: () => at, ttlMs: 1000 })
  cache.set(key(), snapshot(), new Date(at).toISOString())
  at += 999
  assert.ok(cache.get(key()))
  at++
  assert.equal(cache.get(key()), null)
  assert.equal(cache.size, 0)
})

test('跨上海日/月/年边界即清除旧范围，月年也不会沿用昨天的开放桶', () => {
  for (const boundary of ['2026-10-18T16:00:00Z', '2026-10-31T16:00:00Z', '2026-12-31T16:00:00Z']) {
    let at = Date.parse(boundary) - 1
    const cache = createUsageRangeCache({ now: () => at })
    for (const period of ['7d', 'month', 'year'] as const) cache.set(key(period), snapshot(at, key(period)), new Date(at).toISOString())
    assert.equal(cache.size, 3)
    at++
    for (const period of ['7d', 'month', 'year'] as const) assert.equal(cache.get(key(period)), null)
    assert.equal(cache.size, 0)
  }
})

test('重置时刻和账期结束以先到者失效，不沿用旧摘要', () => {
  for (const boundary of ['reset', 'cycle'] as const) {
    let at = INITIAL
    const cache = createUsageRangeCache({ now: () => at })
    const data = snapshot()
    if (boundary === 'reset') data.summary.next_reset_at = new Date(at + 1000).toISOString()
    else data.current_cycle!.ends_at = new Date(at + 1000).toISOString()
    assert.equal(cache.set(key(), data, new Date(at).toISOString()), true)
    at += 1000
    assert.equal(cache.get(key()), null)
    assert.equal(cache.set(key(), data, new Date(at).toISOString()), false)
  }
})

test('成功响应发现账期变更使同服务其他范围失效，旧响应不能倒退新账期', () => {
  let at = INITIAL
  const cache = createUsageRangeCache({ now: () => at })
  const old = snapshot(INITIAL, key('year'))
  cache.set(key('year'), old, new Date(at).toISOString())
  cache.set(key(), snapshot(), new Date(at).toISOString())
  at += 1000
  const current = snapshot(at, key('month'))
  assert.equal(cache.set(key('month'), current, new Date(at).toISOString()), true)
  assert.equal(cache.get(key('year')), null)
  assert.equal(cache.get(key()), null)
  assert.equal(cache.set(key('year'), old, new Date(at).toISOString()), false)
  assert.equal(cache.get(key('month'))?.snapshot, current)
})

test('LRU容量有界，常用范围被保留，覆盖同键不增加占用', () => {
  const cache = createUsageRangeCache({ now: () => INITIAL, maxEntries: 2 })
  for (const period of ['7d', 'month'] as const) cache.set(key(period), snapshot(INITIAL, key(period)), new Date(INITIAL).toISOString())
  cache.get(key())
  cache.set(key('year'), snapshot(INITIAL, key('year')), new Date(INITIAL).toISOString())
  assert.equal(cache.get(key('month')), null)
  assert.ok(cache.get(key()))
  cache.set(key(), snapshot(), new Date(INITIAL).toISOString())
  assert.equal(cache.size, 2)
})

test('权限失效按服务、owner或全局清理，其他身份不受误清理', () => {
  const cache = createUsageRangeCache({ now: () => INITIAL })
  for (const selected of [key(), key('month'), key('year', 'canonical-b'), key('7d', 'canonical-a', 'generation:2')]) cache.set(selected, snapshot(INITIAL, selected), new Date(INITIAL).toISOString())
  cache.clearService('generation:1', 'canonical-a')
  assert.equal(cache.get(key()), null)
  assert.equal(cache.get(key('month')), null)
  assert.equal(cache.size, 2)
  cache.clearOwner('generation:1')
  assert.equal(cache.size, 1)
  assert.ok(cache.get(key('7d', 'canonical-a', 'generation:2')))
  cache.clear()
  assert.equal(cache.size, 0)
})

test('错服务/范围/时区/日期/损坏重置时间与无真实timeseries响应拒存', () => {
  const cache = createUsageRangeCache({ now: () => INITIAL })
  const changes: ((data: UsageOverviewData) => void)[] = [
    data => { data.service_id = 'another' }, data => { data.period = 'month' },
    data => { data.timeseries!.service_id = 'another' }, data => { data.timeseries!.period = 'year' },
    data => { data.timeseries!.range_start = '2026-10-01T00:00:00Z' },
    data => { data.timeseries!.range_end = '2026-10-19T00:00:00Z' },
    data => { data.generated_at = '2026-10-17T00:00:00Z' }, data => { data.timeseries!.as_of = 'invalid' },
    data => { data.summary.next_reset_at = 'invalid' }, data => { delete data.timeseries },
  ]
  for (const change of changes) {
    const data = snapshot(); change(data)
    assert.equal(cache.set(key(), data, new Date(INITIAL).toISOString()), false)
  }
  assert.equal(cache.size, 0)
})

test('服务端ISO秒和上海偏移按同一时刻校验，既不误拒也不重写原采样时间', () => {
  const cache = createUsageRangeCache({ now: () => INITIAL + 1000 })
  const data = snapshot()
  data.generated_at = '2026-10-18T12:00:00+08:00'
  data.timeseries!.as_of = '2026-10-18T12:00:00.123456+08:00'
  data.timeseries!.range_end = '2026-10-18T04:00:00.123Z'
  data.timeseries!.range_start = '2026-10-12T00:00:00+08:00'
  assert.equal(cache.set(key(), data, '2026-10-18T04:00:01Z'), true)
  assert.equal(cache.get(key())?.snapshot.timeseries?.as_of, '2026-10-18T12:00:00.123456+08:00')
})

test('失败或迟到旧响应不改变已缓存快照与原时间，本机时钟倒退要求重验', () => {
  let at = INITIAL
  const cache = createUsageRangeCache({ now: () => at })
  const data = snapshot()
  cache.set(key(), data, new Date(at).toISOString())
  const previous = cache.get(key())
  at += 500
  assert.deepEqual(cache.get(key()), previous)
  const older = snapshot(INITIAL - 1000)
  older.current_cycle = data.current_cycle
  assert.equal(cache.set(key(), older, new Date(at).toISOString()), false)
  assert.deepEqual(cache.get(key()), previous)
  at = INITIAL - 1
  assert.equal(cache.get(key()), null)
})

test('非法内存预算拒绝，空身份/服务不建立可复用缓存', () => {
  for (const ttlMs of [0, -1, Infinity, NaN]) assert.throws(() => createUsageRangeCache({ ttlMs }), RangeError)
  for (const maxEntries of [0, -1, 1.5, Infinity]) assert.throws(() => createUsageRangeCache({ maxEntries }), RangeError)
  const cache = createUsageRangeCache({ now: () => INITIAL })
  assert.equal(cache.set(key('7d', 'canonical-a', ''), snapshot(), new Date(INITIAL).toISOString()), false)
  assert.equal(cache.get(key('7d', '')), null)
})
