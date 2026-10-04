import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'
import * as usage from '../src/providerUsage.ts'
import type { ProviderUsage, ResourceUsageMeter } from '../src/providerUsage.ts'

const currentTime = Date.parse('2026-10-04T06:59:00Z')
const meter = (changes: Partial<ResourceUsageMeter> = {}): ResourceUsageMeter => ({
  id: 'resource-fixture', label: '示例资源', scope: 'external_node', source_kind: 'estimate', quality: 'current',
  observed_at: '2026-10-04T06:58:00Z', expires_at: '2026-10-04T07:00:00Z',
  quota_bytes: '400000000000', used_bytes: '73538040918', remaining_bytes: '326461959082',
  upload_bytes: '23905557275', download_bytes: '49632483643',
  cycle: { kind: 'fixed_days', starts_at: '2026-09-20T00:00:00+08:00', ends_at: '2026-10-20T00:00:00+08:00', next_reset_at: '2026-10-20T00:00:00+08:00' },
  expires_on: '2027-01-19', alerts: [], details: {}, ...changes,
})
const payload = (meters = [meter()]): ProviderUsage => ({ schema_version: 2, generated_at: '2026-10-04T06:59:00Z', meters })

test('来源各自时效，响应生成时间不能延长样本有效期', () => {
  assert.equal(usage.providerQuality(meter(), currentTime), 'current')
  assert.equal(usage.providerQuality(meter(), Date.parse('2026-10-04T07:00:00Z')), 'stale')
  assert.equal(usage.providerQuality(meter({ expires_at: null }), currentTime), 'stale')
  assert.equal(usage.providerQuality(meter({ observed_at: null }), currentTime), 'stale')
  for (const quality of ['error', 'gap', 'missing'] as const) assert.equal(usage.providerQuality(meter({ quality }), 0), quality)
})

test('缺口和错误样本过期追加独立提醒，保留后端事实并去重', () => {
  const later = currentTime + 24 * 60 * 60 * 1000
  for (const quality of ['gap', 'error'] as const) {
    const value = meter({ quality, remaining_bytes: null, alerts: [{ code: 'collection_gap', severity: 'warning', message: '采集存在缺口' }] })
    const original = structuredClone(value)
    assert.equal(usage.providerQuality(value, later), quality)
    assert.deepEqual(usage.resourceAlerts(value, currentTime).map(alert => alert.code), ['collection_gap'])
    const alerts = usage.resourceAlerts(value, later)
    assert.deepEqual(alerts.map(alert => alert.code), ['collection_gap', 'source_stale'])
    assert.deepEqual(usage.resourceAlerts({ ...value, alerts }, later), alerts)
    assert.deepEqual(value, original)
  }
  assert.deepEqual(usage.resourceAlerts(meter({ quality: 'missing', observed_at: null }), later), [])
  assert.deepEqual(usage.resourceAlerts(meter({ quality: 'gap', expires_at: null }), later), [])
})

test('大整数独立占比、超额保留数值、无额度不画假进度', () => {
  assert.equal(usage.billingPercent(meter({ used_bytes: '90071992547409930000', quota_bytes: '180143985094819860000' })), 50)
  assert.equal(usage.billingPercent(meter({ used_bytes: '500000000000' })), 100)
  assert.equal(usage.billingPercent(meter({ quota_bytes: null })), null)
  assert.equal(usage.billingPercent(meter({ quota_bytes: '0' })), null)
  assert.equal(usage.billingPercent(meter({ used_bytes: null })), null)
  assert.equal(usage.resourceBytes('9007199254740993'), 9007199254740993n)
  for (const bad of [null, undefined, '-1', '1e3', '', 1000, '01']) assert.equal(usage.resourceBytes(bad), null)
  assert.equal(usage.resourceBytesLabel('1'), '< 0.01 GB')
  assert.equal(usage.resourceBytesLabel('0'), '0 GB')
  assert.equal(usage.resourceBytesLabel(null), '暂无记录')
})

test('上下行只有已知才展示构成，不能用一份额度推算另一份余额', () => {
  assert.equal(usage.transferShare(meter({ upload_bytes: '10', download_bytes: '30' })), 25)
  assert.equal(usage.residentialShare(meter({ upload_bytes: null })), null)
  assert.equal(usage.transferShare(meter({ upload_bytes: '0', download_bytes: '0' })), null)
  const first = meter(), second = meter({ remaining_bytes: '9000000000', scope: 'server', source_kind: 'official' })
  assert.equal(first.remaining_bytes, '326461959082')
  assert.equal(second.remaining_bytes, '9000000000')
})

test('日期精度原样显示，未知不是待开通，日期不会当重置时间', () => {
  assert.equal(usage.resourceDateLabel('2027-01-19'), '2027-01-19')
  assert.equal(usage.resourceDateLabel('2026-02-31'), '未提供')
  assert.equal(usage.resourceDateLabel(null), '未提供')
  assert.equal(usage.resourceTimeLabel(null), '未提供')
  assert.equal(usage.resourceTimestamp('2026-10-20T00:00:00'), null)
  assert.equal(usage.resourceScopeLabel('machine'), '整机流量')
  assert.equal(usage.resourceScopeLabel('unknown-source'), '资源统计')
})

const transpile = (source: string) => ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const components = new Map<string, any>()
function loadComponent(filename: string): any {
  if (components.has(filename)) return components.get(filename)
  const descriptor = parse(readFileSync(new URL('../src/components/' + filename, import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: filename, inlineTemplate: true })
  const exports: Record<string, any> = {}
  new Function('require', 'exports', transpile(script.content))((name: string) => {
    if (name === 'vue') return vue
    if (name === '../providerUsage') return usage
    if (name === './ResourceUsageCard.vue') return { default: loadComponent('ResourceUsageCard.vue') }
    throw new Error('未声明的测试依赖：' + name)
  }, exports)
  components.set(filename, exports.default)
  return exports.default
}
const Card = loadComponent('ResourceUsageCard.vue'), Dashboard = loadComponent('ProviderUsageDashboard.vue')
const renderCard = (value: ResourceUsageMeter, now = currentTime) => renderToString(vue.createSSRApp(Card, { meter: value, now }))

test('同一真实卡片显示估算额度已用剩余，迁入缺口只放详情', async () => {
  const html = await renderCard(meter({ label: '自定义外部节点', details: { accepted_historical_gap: true } }))
  assert.match(html, /自定义外部节点/)
  assert.match(html, /估算/)
  assert.match(html, /73.53 GB/)
  assert.match(html, /400 GB/)
  assert.match(html, /326.46 GB/)
  assert.match(html, /2027-01-19/)
  assert.match(html, /2026[^<]*10[^<]*20/)
  assert.doesNotMatch(html, /开始记录前的用量未计入|不推算本月剩余|待开通/)
  assert.doesNotMatch(html, /class="resource-alerts"/)
  assert.match(html, /<details[^>]*class="resource-details"[^>]*>/)
  assert.doesNotMatch(html, /<details[^>]* open/)
  const beforeDetails = html.split('<details')[0]!
  assert.doesNotMatch(beforeDetails, /迁入前缺口|历史不完整/)
})

test('整机官方数据仍是独立资源卡，不改成个人套餐或合计', async () => {
  const html = await renderCard(meter({ label: '我的机器', scope: 'server', source_kind: 'official', upload_bytes: null, download_bytes: null }))
  assert.match(html, /我的机器/)
  assert.match(html, /服务器流量/)
  assert.match(html, /官方/)
  assert.doesNotMatch(html, /<dt>上传<|<dt>下载<|个人套餐/)
})

test('未知额度和缺失指标不补零，没有到期日期不意味着等待开通', async () => {
  const html = await renderCard(meter({ quota_bytes: null, remaining_bytes: null, upload_bytes: null, download_bytes: '0', expires_on: null, cycle: null }))
  assert.match(html, /73.53 GB/)
  assert.match(html, /暂不可确认/)
  assert.match(html, /<dt>额度<\/dt><dd>未提供<\/dd>/)
  assert.doesNotMatch(html, /class="quota-track/)
  assert.doesNotMatch(html, /<dt>上传</)
  assert.match(html, /<dt>下载<\/dt><dd>0 GB<\/dd>/)
  assert.doesNotMatch(html, /待开通|永久有效|不限流量/)
})

test('过期自动降级并保留上次值，恢复新样本后异常提醒消失', async () => {
  const expired = await renderCard(meter(), Date.parse('2026-10-04T07:00:00Z'))
  assert.match(expired, /更新延迟/)
  assert.match(expired, /上次记录剩余/)
  assert.match(expired, /326.46 GB/)
  const failed = await renderCard(meter({ quality: 'error', alerts: [{ code: 'source_error', severity: 'error', message: '来源更新失败' }] }))
  assert.match(failed, /更新失败/)
  assert.match(failed, /来源更新失败/)
  const restored = await renderCard(meter({ expires_at: '2026-10-04T08:00:00Z' }))
  assert.doesNotMatch(restored, /更新失败|来源更新失败|上次记录剩余/)
})

test('缺口卡24小时后仍显示缺口和未知剩余，同时提示更新延迟', async () => {
  const value = meter({ quality: 'gap', remaining_bytes: null, alerts: [{ code: 'collection_gap', severity: 'warning', message: '采集存在缺口' }] })
  const initial = await renderCard(value)
  assert.doesNotMatch(initial, /更新延迟/)
  const later = await renderCard(value, currentTime + 24 * 60 * 60 * 1000)
  assert.match(later, /统计存在缺口/)
  assert.match(later, /采集存在缺口/)
  assert.match(later, /更新延迟/)
  assert.match(later, /暂不可确认/)
  assert.match(later, /73.53 GB/)
  assert.equal(value.quality, 'gap')
  assert.equal(value.remaining_bytes, null)
})

test('空列表和读取失败都有统一可读状态，schema1不会冒充新资源卡', async () => {
  const empty = await renderToString(vue.createSSRApp(Dashboard, { data: payload([]) }))
  assert.match(empty, /暂无资源用量记录/)
  const failed = await renderToString(vue.createSSRApp(Dashboard, { data: { ...payload([]), error: { code: 'snapshot_missing', message: '用量资料尚未同步' } } }))
  assert.match(failed, /用量资料尚未同步/)
  assert.doesNotMatch(failed, /0 GB|暂无资源用量记录/)
  const old = await renderToString(vue.createSSRApp(Dashboard, { data: { schema_version: 1 } }))
  assert.match(old, /暂无资源用量记录/)
})

test('仪表盘按数组渲染任意资源名称，不硬编码HOME或BWH分支', async () => {
  const html = await renderToString(vue.createSSRApp(Dashboard, { data: payload([meter({ id: 'one', label: '资源甲' }), meter({ id: 'two', label: '资源乙', scope: 'server', source_kind: 'official' })]) }))
  assert.match(html, /资源甲/)
  assert.match(html, /资源乙/)
  assert.equal((html.match(/class="resource-usage-card"/g) || []).length, 2)
  assert.match(html, /各资源分别统计，不合并为个人套餐用量/)
  assert.doesNotMatch(html, /HOME|BWH|住宅出口/)
})

test('单一刷新时钟到期更新，卸载清理且SSR不创建定时器', async () => {
  const descriptor = parse(readFileSync(new URL('../src/components/ProviderUsageDashboard.vue', import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: 'clock-test' }), exports: Record<string, any> = {}
  const starts: Array<() => void> = [], ends: Array<() => void> = []
  new Function('require', 'exports', transpile(script.content))((name: string) => name === 'vue' ? { ...vue, onMounted: (fn: () => void) => starts.push(fn), onBeforeUnmount: (fn: () => void) => ends.push(fn) } : { default: Card }, exports)
  const oldInterval = globalThis.setInterval, oldClear = globalThis.clearInterval, oldNow = Date.now
  let tick: (() => void) | undefined, scheduled = 0, cleared = 0, clock = currentTime
  globalThis.setInterval = ((fn: () => void) => { scheduled++; tick = fn; return 42 }) as unknown as typeof setInterval
  globalThis.clearInterval = (() => { cleared++ }) as typeof clearInterval
  Date.now = () => clock
  try {
    const state = exports.default.setup({ data: payload() }, { expose() {} })
    assert.equal(scheduled, 0)
    starts.forEach(fn => fn())
    assert.equal(scheduled, 1)
    clock = Date.parse('2026-10-04T07:00:00Z'); tick!()
    assert.equal(usage.providerQuality(meter(), state.now.value), 'stale')
    const gap = meter({ quality: 'gap', remaining_bytes: null })
    clock = currentTime + 24 * 60 * 60 * 1000; tick!()
    assert.equal(usage.providerQuality(gap, state.now.value), 'gap')
    assert.equal(usage.resourceAlerts(gap, state.now.value).filter(alert => alert.code === 'source_stale').length, 1)
    assert.equal(gap.remaining_bytes, null)
    ends.forEach(fn => fn()); assert.equal(cleared, 1)
  } finally { globalThis.setInterval = oldInterval; globalThis.clearInterval = oldClear; Date.now = oldNow }
})
