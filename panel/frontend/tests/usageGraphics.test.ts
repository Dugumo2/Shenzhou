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
import * as api from '../src/api.ts'
import type { UsageOverviewData } from '../src/usage-types.ts'
import type { ResourceUsageMeter } from '../src/providerUsage.ts'

const require = createRequire(import.meta.url)
const sourceRoot = fileURLToPath(new URL('../src/', import.meta.url))

function overview(): UsageOverviewData {
  return {
    service_id: 'graphics-fixture', source_type: 'entitlement', time_zone: 'Asia/Shanghai',
    generated_at: '2026-10-03T01:00:00Z', period: 'current',
    current_cycle: { starts_at: '2026-09-30T16:00:00Z', ends_at: '2026-10-31T16:00:00Z' },
    summary: { quota_bytes: '100000000000', quota_state: 'applied', charged_bytes: '30000000000', remaining_bytes: '70000000000', upload_bytes: '10000000000', download_bytes: '20000000000', next_reset_at: '2026-10-31T16:00:00Z' },
    quality: { state: 'measured', message: '合成样本', collected_at: '2026-10-03T00:59:00Z' },
    history: { kind: 'confirmed_ledger_postings', date_basis: 'created_at', range_start: '2026-09-30T16:00:00Z', range_end: '2026-10-31T16:00:00Z', totals: { charged_bytes: '30000000000', upload_bytes: '10000000000', download_bytes: '20000000000' }, record_count: 2, excluded_record_count: 0, day_limit: 90, days_truncated: false, message: '', days: [
      { date: '2026-10-01', record_count: 1, charged_bytes: '30000000000', upload_bytes: '10000000000', download_bytes: '20000000000' },
      { date: '2026-10-03', record_count: 1, charged_bytes: '0', upload_bytes: '0', download_bytes: '0' },
    ] },
  }
}

function meter(changes: Partial<ResourceUsageMeter> = {}): ResourceUsageMeter {
  return { id: 'resource-one', label: '资源甲', scope: 'external_node', source_kind: 'estimate', quality: 'current',
    quota_bytes: '400000000000', used_bytes: '73000000000', remaining_bytes: '327000000000', upload_bytes: '23000000000', download_bytes: '50000000000',
    observed_at: '2026-10-07T01:00:00Z', expires_at: '2099-10-07T02:00:00Z',
    cycle: { kind: 'fixed_days', starts_at: '2026-09-20T00:00:00+08:00', ends_at: '2026-10-20T00:00:00+08:00', next_reset_at: '2026-10-20T00:00:00+08:00' }, expires_on: '2027-01-19', alerts: [], details: {}, ...changes }
}

// 编译并渲染真实概览及其真实子组件，仅替换接口响应；不复制模板或重写来源分支。
async function renderEntry(entry: string, props: Record<string, unknown>, responses: Record<string, unknown>) {
  const cache = new Map<string, any>(), requests: string[] = []
  const auth = vue.reactive({ session: { authenticated: true, user: { username: 'graphics-user', is_staff: false }, csrf_token: 'fixture-token' } })
  function load(filename: string): any {
    if (cache.has(filename)) return cache.get(filename)
    const source = readFileSync(filename, 'utf8')
    const content = filename.endsWith('.vue')
      ? compileScript(parse(source).descriptor, { id: filename, inlineTemplate: true }).content : source
    const code = ts.transpileModule(content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
    const exports: Record<string, any> = {}
    cache.set(filename, exports)
    new Function('require', 'exports', code)((name: string) => {
      if (name === 'vue') return vue
      if (/\/api(?:\.ts)?$/.test(name)) return { ...api, request: async (path: string) => {
        requests.push(path)
        assert.ok(path in responses, '未预期的读取：' + path)
        return responses[path]
      } }
      if (/\/auth(?:\.ts)?$/.test(name)) return { auth }
      if (name === 'vue-router') return { useRoute: () => ({ params: { id: 'graphics-fixture' } }) }
      if (!name.startsWith('.')) return require(name)
      return load(resolve(dirname(filename), /\.(vue|ts)$/.test(name) ? name : name + '.ts'))
    }, exports)
    return exports
  }
  const component = load(resolve(sourceRoot, entry)).default
  const wrapper = { ...component, async setup(props: any, context: any) {
    const render = component.setup(props, context)
    for (let i = 0; i < 40; i++) await Promise.resolve()
    return render
  } }
  const app = vue.createSSRApp(wrapper, props)
  for (const name of ['el-button', 'el-alert', 'el-skeleton', 'el-tag', 'el-radio-group', 'el-radio-button', 'el-tabs', 'el-tab-pane', 'el-progress']) {
    app.component(name, { setup(_props: unknown, { slots }: any) { return () => vue.h('div', slots.default?.()) } })
  }
  app.component('RouterLink', { props: ['to'], setup(props: any, { slots }: any) { return () => vue.h('a', { href: props.to }, slots.default?.()) } })
  const html = await renderToString(app)
  return { html, requests }
}

async function renderOverview(data: UsageOverviewData, summaryOnly = false) {
  const { html, requests } = await renderEntry('components/UsageOverview.vue', { serviceId: data.service_id, summaryOnly }, { '/me/services/graphics-fixture/usage?period=current': data })
  assert.deepEqual(requests, ['/me/services/graphics-fixture/usage?period=current'])
  return html
}

test('实际概览收到来源响应后保留每项资源圆环、已有上下行构成及资源日期', async () => {
  const data = overview()
  data.source_type = 'p8'
  data.provider_usage = { schema_version: 2, generated_at: data.generated_at, meters: [meter(), meter({ id: 'resource-two', label: '资源乙', scope: 'server', source_kind: 'official', upload_bytes: null, download_bytes: null })] }
  for (const compact of [true, false]) {
    const html = await renderOverview(data, compact)
    assert.equal((html.match(/data-chart="quota-gauge"/g) || []).length, 2)
    assert.equal((html.match(/data-chart="transfer-composition"/g) || []).length, 1)
    for (const value of ['资源甲', '资源乙', '400 GB', '73 GB', '327 GB', '估算', '官方', '下次重置', '到期日期', '最后更新', '2027-01-19']) assert.ok(html.includes(value), value)
    assert.doesNotMatch(html, /套餐额度|class="history-chart"|class="actual-bar/)
    if (!compact) assert.match(html, /尚未提供可用于绘制趋势的历史采样/)
  }
})

test('实际个人套餐概览保留额度和传输图，完整分区保留真实记录图及详情入口', async () => {
  const data = overview(), compact = await renderOverview(data, true), full = await renderOverview(data)
  for (const html of [compact, full]) {
    assert.match(html, /data-chart="quota-gauge"/)
    assert.match(html, /data-chart="transfer-composition"/)
    assert.match(html, /当前已用占比 30%/)
    assert.match(html, /本期原始传输量，未乘流量倍率/)
  }
  assert.doesNotMatch(compact, /class="history-chart"/)
  assert.match(full, /class="history-chart"/)
  assert.match(full, /查看详细统计/)
  assert.match(full, /2026-10-02：暂无已确认记录，不代表零流量/)
  assert.match(full, /2026-10-03：0 GB/)
  assert.match(full, /下次流量重置/)
  assert.match(full, /最后采集/)
})

test('实际来源错误和缺失保留异常与已知图形，未知资源不伪造百分比或零值', async () => {
  const data = overview()
  data.provider_usage = { schema_version: 2, generated_at: data.generated_at, meters: [meter({ quality: 'error', alerts: [{ code: 'source_error', severity: 'error', message: '来源暂时无法更新' }] })] }
  const failed = await renderOverview(data)
  assert.match(failed, /来源暂时无法更新/)
  assert.match(failed, /data-chart="quota-gauge"/)
  assert.match(failed, /data-chart="transfer-composition"/)
  assert.match(failed, /上次记录剩余/)
  data.provider_usage.meters = [meter({ quality: 'missing', quota_bytes: null, used_bytes: null, remaining_bytes: null, upload_bytes: null, download_bytes: null, observed_at: null, expires_at: null })]
  const missing = await renderOverview(data)
  assert.match(missing, /暂无可计算比例/)
  assert.doesNotMatch(missing, /data-chart="transfer-composition"|0 GB|conic-gradient\(/)
})

test('真实服务父页面共享一份用量读取，受控概览与流量分区均保留来源图形', async () => {
  const data = overview()
  data.source_type = 'p8'
  data.provider_usage = { schema_version: 2, generated_at: data.generated_at, meters: [meter(), meter({ id: 'resource-two', label: '资源乙', source_kind: 'official', scope: 'server', quota_bytes: '2000000000000', used_bytes: '170000000000', remaining_bytes: '1830000000000', upload_bytes: null, download_bytes: null })] }
  const service = { id: data.service_id, name: '神舟云', source_type: 'p8', provider_usage: data.provider_usage,
    quota_bytes: null, used_bytes: null, raw_bytes: null, remaining_bytes: null, next_reset_at: null, expires_at: null,
    state: 'active', enabled: true, status_label: '使用中', business_state: 'active', quota_state: 'unknown',
    usage: { quality: 'unknown', updated_at: null, message: '没有个人套餐账本' },
    delivery: { state: 'unknown', message: '', download_url: null } }
  const responses = {
    '/me/services/graphics-fixture': service,
    '/me/services/graphics-fixture/usage?period=current': data,
    '/catalog/clients': { items: [] },
    '/me/services': { items: [service], compatibility: { state: 'ready', message: null } },
  }
  const detail = await renderEntry('pages/ServicePage.vue', {}, responses)
  assert.deepEqual([...detail.requests].sort(), ['/catalog/clients', '/me/services/graphics-fixture', '/me/services/graphics-fixture/usage?period=current'])
  // 标签页宿主展开两个真实 slot，分别核对概览和完整用量分区的受控挂载。
  assert.equal((detail.html.match(/data-chart="quota-gauge"/g) || []).length, 4)
  assert.equal((detail.html.match(/data-chart="transfer-composition"/g) || []).length, 2)
  assert.equal((detail.html.match(/class="refresh-control/g) || []).length, 1)
  assert.match(detail.html, /尚未提供可用于绘制趋势的历史采样/)
  assert.match(detail.html, /按资源分别计算/)
  const list = await renderEntry('pages/ServicesPage.vue', {}, responses)
  assert.deepEqual(list.requests, ['/me/services'])
  assert.match(list.html, /href="\/services\/graphics-fixture"/)
  for (const html of [list.html, detail.html]) {
    for (const value of ['资源甲', '资源乙', '400 GB', '73 GB', '327 GB', '2,000 GB', '170 GB', '1,830 GB', '2027-01-19', '估算', '官方']) assert.ok(html.includes(value), value)
    assert.doesNotMatch(html, /暂无可靠统计|2,400 GB|243 GB/)
  }
})
