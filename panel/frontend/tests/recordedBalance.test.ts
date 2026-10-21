import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse, compileScript } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'
import { serviceUsage, serviceRemaining, formatGB } from '../src/display.ts'
import { quotaGauge, transferShare } from '../src/usageCharts.ts'
import { reliableUsagePercent } from '../src/usage-types.ts'
import type { Service, Bytes } from '../src/types.ts'
import type { UsageOverviewData } from '../src/usage-types.ts'

function service(): Service {
  return { id: 'recorded-balance-fixture', name: '套餐', quota_bytes: '1000000000000', quota_state: 'applied',
    used_bytes: null, remaining_bytes: null, raw_bytes: null, recorded_used_bytes: '5690000000',
    recorded_remaining_bytes: '994310000000', recorded_upload_bytes: '2000000000', recorded_download_bytes: '3690000000',
    usage_estimate: null, next_reset_at: '2026-11-20T00:00:00+08:00', expires_at: '2027-12-30T00:00:00+08:00',
    state: 'active', enabled: true, status_label: '已应用', source_type: 'entitlement',
    usage: { quality: 'gap', updated_at: '2026-10-21T12:00:00+08:00', message: '部分观测' },
    delivery: { state: 'ready', message: '', download_url: null } }
}

function overview(row = service()): UsageOverviewData {
  return { service_id: row.id, source_type: 'entitlement', time_zone: 'Asia/Shanghai',
    generated_at: '2026-10-21T12:00:00+08:00', period: 'current',
    current_cycle: { starts_at: '2026-10-20T00:00:00+08:00', ends_at: row.next_reset_at! },
    summary: { quota_bytes: String(row.quota_bytes), quota_state: row.quota_state!, charged_bytes: row.used_bytes as string | null,
      remaining_bytes: row.remaining_bytes as string | null, recorded_used_bytes: row.recorded_used_bytes as string | null,
      recorded_remaining_bytes: row.recorded_remaining_bytes as string | null,
      recorded_upload_bytes: row.recorded_upload_bytes as string | null, recorded_download_bytes: row.recorded_download_bytes as string | null,
      upload_bytes: row.recorded_upload_bytes as string | null, download_bytes: row.recorded_download_bytes as string | null,
      usage_estimate: row.usage_estimate, next_reset_at: row.next_reset_at },
    quality: { state: 'gap', message: '部分观测', collected_at: row.usage.updated_at },
    history: { kind: 'confirmed_ledger_postings', date_basis: 'created_at', range_start: '2026-10-20T00:00:00+08:00',
      range_end: '2026-10-21T12:00:00+08:00', totals: null, record_count: 0, excluded_record_count: 0,
      days: [], day_limit: 90, days_truncated: false, message: '' } }
}

test('换期后无一次衔接，部分入账仍显示普通套餐已用及账本剩余', () => {
  const row = service(), data = overview(row), before = JSON.stringify([row, data])
  assert.equal(serviceUsage(row).label, '套餐已用')
  assert.equal(formatGB(serviceUsage(row).bytes), '5.69 GB')
  assert.equal(formatGB(serviceRemaining(row)), '994.31 GB')
  assert.equal(quotaGauge(data).percent, 0.56)
  assert.equal(transferShare(data)?.total, '5690000000')
  assert.equal(reliableUsagePercent(data), null)
  assert.equal(JSON.stringify([row, data]), before)
})

test('原期合法一次衔接优先，下期服务与图表只读新账期入账', () => {
  const row = service()
  row.usage_estimate = { state: 'estimated', used_bytes: '98253546067', remaining_bytes: '901746453933',
    upload_bytes: '33734964678', download_bytes: '64518581389' }
  assert.equal(serviceUsage(row).bytes, '98253546067')
  assert.equal(serviceRemaining(row), '901746453933')
  assert.equal(quotaGauge(overview(row)).percent, 9.82)
  assert.equal(transferShare(overview(row))?.total, '98253546067')
  row.usage_estimate = null
  assert.equal(serviceUsage(row).bytes, '5690000000')
  assert.equal(serviceRemaining(row), '994310000000')
  assert.equal(quotaGauge(overview(row)).percent, 0.56)
})

test('已确认真零与超额仍有明确余额，原可靠字段保持未知', () => {
  for (const [used, remaining, percent] of [['0', '1000000000000', 0], ['1200000000000', '0', 100]] as const) {
    const row = service(); row.recorded_used_bytes = used; row.recorded_remaining_bytes = remaining
    assert.equal(serviceUsage(row).bytes, used)
    assert.equal(serviceRemaining(row), remaining)
    assert.equal(quotaGauge(overview(row)).percent, percent)
    assert.equal(row.used_bytes, null); assert.equal(row.remaining_bytes, null)
    assert.equal(reliableUsagePercent(overview(row)), null)
  }
})

test('缺记录、未应用额度和非法或混源余额均不能伪造剩余', () => {
  const missing = service(); missing.recorded_used_bytes = null; missing.recorded_remaining_bytes = null
  assert.equal(serviceRemaining(missing), null); assert.equal(quotaGauge(overview(missing)).percent, null)
  const configured = service(); configured.quota_state = 'configured'
  assert.equal(serviceRemaining(configured), null)
  for (const invalid of ['-1', 'NaN', '5.5', Number.MAX_SAFE_INTEGER + 1, '999999999999'] as Bytes[]) {
    const row = service(); row.recorded_remaining_bytes = invalid
    assert.equal(serviceRemaining(row), null, String(invalid))
  }
  const mismatch = service(); mismatch.recorded_used_bytes = null
  assert.equal(serviceRemaining(mismatch), null)
})

test('余额字段不可让缺口变成可靠比例，完整计量路径仍用其原金额', () => {
  const row = service(); row.used_bytes = '6000000000'; row.remaining_bytes = '994000000000'; row.usage.quality = 'measured'
  const data = overview(row); data.quality.state = 'measured'
  assert.equal(serviceUsage(row).bytes, '6000000000')
  assert.equal(serviceRemaining(row), '994000000000')
  assert.equal(quotaGauge(data).percent, 0.6)
  data.quality.state = 'gap'
  assert.equal(reliableUsagePercent(data), null)
})

// 编译真实仪表盘和依赖；不复制模板，SSR只证明内容与图形，不冒充浏览器视觉验收。
async function renderDashboard(data: UsageOverviewData) {
  const require = createRequire(import.meta.url), cache = new Map<string, any>()
  function load(filename: string): any {
    if (cache.has(filename)) return cache.get(filename)
    const source = readFileSync(filename, 'utf8')
    const content = filename.endsWith('.vue') ? compileScript(parse(source).descriptor, { id: filename, inlineTemplate: true }).content : source
    const code = ts.transpileModule(content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
    const exports: Record<string, any> = {}; cache.set(filename, exports)
    new Function('require', 'exports', code)((name: string) => {
      if (name === 'vue') return vue
      if (!name.startsWith('.')) return require(name)
      return load(resolve(dirname(filename), /\.(vue|ts)$/.test(name) ? name : name + '.ts'))
    }, exports)
    return exports
  }
  const filename = fileURLToPath(new URL('../src/components/UsageDashboard.vue', import.meta.url))
  const app = vue.createSSRApp(load(filename).default, { data, compact: true })
  for (const name of ['el-radio-button', 'el-radio-group']) {
    app.component(name, { setup(_props: unknown, { slots }: any) { return () => vue.h('div', slots.default?.()) } })
  }
  return renderToString(app)
}

test('真实仪表盘新期保留额度圆环和上传下载，普通金额不退回待核算', async () => {
  const html = await renderDashboard(overview())
  for (const text of ['套餐已用', '剩余流量', '5.69 GB', '994.31 GB', '上传', '下载']) assert.ok(html.includes(text), text)
  for (const chart of ['quota-gauge', 'transfer-composition']) assert.equal((html.match(new RegExp('data-chart="' + chart + '"', 'g')) || []).length, 1)
  assert.doesNotMatch(html, /待核算|估算|迁入|灰色部分不代表当前可用余额/)
})

test('真实仪表盘无确认行保留未知，不借额度生成真实零', async () => {
  const row = service(); row.recorded_used_bytes = null; row.recorded_remaining_bytes = null
  row.recorded_upload_bytes = null; row.recorded_download_bytes = null
  const html = await renderDashboard(overview(row))
  assert.match(html, /待核算/)
  assert.doesNotMatch(html, /已确认记录为零|994.31 GB|0%/)
})

test('采样过期保留旧完整值时，套餐数字圆环及余额仍选同份当前账本', async () => {
  const row = service(); row.used_bytes = '4000000000'; row.usage.quality = 'stale'
  const data = overview(row); data.quality.state = 'stale'
  assert.equal(serviceUsage(row).bytes, '5690000000')
  assert.equal(serviceRemaining(row), '994310000000')
  assert.equal(quotaGauge(data).percent, 0.56)
  assert.equal(reliableUsagePercent(data), null)
  const html = await renderDashboard(data)
  assert.match(html, /套餐已用/); assert.match(html, /5.69 GB/); assert.match(html, /994.31 GB/)
  assert.doesNotMatch(html, /<dd>4 GB<\/dd>/)
})

test('已应用零额度的真实余额仍为零，圆环不计算零分母比例', async () => {
  const row = service(); row.quota_bytes = '0'; row.recorded_remaining_bytes = '0'
  const data = overview(row)
  assert.equal(serviceRemaining(row), '0')
  assert.equal(serviceUsage(row).bytes, '5690000000')
  assert.equal(quotaGauge(data).percent, null)
  assert.equal(reliableUsagePercent(data), null)
  const html = await renderDashboard(data)
  assert.match(html, /<dt>剩余流量<\/dt><dd>0 GB<\/dd>/)
  assert.match(html, /套餐已用/); assert.match(html, /5.69 GB/)
  assert.doesNotMatch(html, /待核算|NaN|Infinity|0%/)
})
