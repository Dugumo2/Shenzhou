import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import { reliableUsagePercent } from '../src/usage-types.ts'
import type { UsageOverviewData } from '../src/usage-types.ts'

function overview(): UsageOverviewData {
  return { service_id: 'synthetic-service', source_type: 'entitlement', time_zone: 'Asia/Shanghai',
    generated_at: '2028-01-10T12:00:00+08:00', period: 'current', current_cycle: null,
    summary: { quota_bytes: '100000000000', quota_state: 'applied', charged_bytes: '30000000000',
      upload_bytes: '12000000000', download_bytes: '12000000000', remaining_bytes: '70000000000', next_reset_at: null },
    quality: { state: 'measured', message: '合成统计', collected_at: null },
    history: { kind: 'confirmed_ledger_postings', date_basis: 'created_at', range_start: null, range_end: null,
      totals: null, record_count: 0, excluded_record_count: 0, days: [], day_limit: 90, days_truncated: false, message: '' } }
}

test('只有可靠剩余和已应用额度才能绘制本期扣费进度', () => {
  assert.equal(reliableUsagePercent(overview()), 30)
  for (const state of ['stale', 'gap', 'unknown'] as const) {
    const data = overview(); data.quality.state = state
    assert.equal(reliableUsagePercent(data), null)
  }
  const configured = overview(); configured.summary.quota_state = 'configured'
  assert.equal(reliableUsagePercent(configured), null)
  const unknown = overview(); unknown.summary.remaining_bytes = null
  assert.equal(reliableUsagePercent(unknown), null)
})

test('不一致或无效字节不绘制进度，零额度不除零', () => {
  for (const value of ['-1', '坏数据', null]) {
    const data = overview(); data.summary.charged_bytes = value
    assert.equal(reliableUsagePercent(data), null)
  }
  const inconsistent = overview(); inconsistent.summary.remaining_bytes = '80000000000'
  assert.equal(reliableUsagePercent(inconsistent), null)
  const zero = overview(); zero.summary.quota_bytes = '0'; zero.summary.charged_bytes = '0'; zero.summary.remaining_bytes = '0'
  assert.equal(reliableUsagePercent(zero), null)
})

test('超过JavaScript安全整数仍能计算，耗尽进度封顶100%', () => {
  const data = overview()
  data.summary.quota_bytes = '18014398509481986'; data.summary.charged_bytes = '9007199254740993'; data.summary.remaining_bytes = '9007199254740993'
  assert.equal(reliableUsagePercent(data), 50)
  data.summary.charged_bytes = '18014398509481987'; data.summary.remaining_bytes = '0'
  assert.equal(reliableUsagePercent(data), 100)
})

// 编译真实组件的setup，仅替换网络及生命周期宿主，核验跨服务迟到响应。
const descriptor = parse(readFileSync(new URL('../src/components/UsageOverview.vue', import.meta.url), 'utf8')).descriptor
const compiled = compileScript(descriptor, { id: 'usage-real-component' })
const js = ts.transpileModule(compiled.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const require = createRequire(import.meta.url)
function mount(send: (path: string) => Promise<UsageOverviewData>, managed = false) {
  const scope = vue.effectScope(), cache = new Map<string, any>()
  const props = vue.reactive({ serviceId: 'service-a', refreshKey: 0, managed, snapshot: null as UsageOverviewData | null, loading: false, loadError: '' })
  const auth = vue.reactive({ session: { authenticated: true, user: { username: 'fixture-a', is_staff: false }, csrf_token: 'fixture-token' } })
  const events: Array<[string, unknown]> = []
  function load(filename: string, compiledCode?: string): any {
    if (cache.has(filename)) return cache.get(filename)
    const exports: Record<string, any> = {}; cache.set(filename, exports)
    const code = compiledCode || ts.transpileModule(readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
    new Function('require', 'exports', code)((name: string) => {
      if (name === 'vue') return vue
      if (name.endsWith('.vue')) return {}
      if (/\/api(?:\.ts)?$/.test(name)) return { ...api, request: send }
      if (/\/auth(?:\.ts)?$/.test(name)) return { auth }
      if (!name.startsWith('.')) return require(name)
      return load(resolve(dirname(filename), name.endsWith('.ts') ? name : name + '.ts'))
    }, exports)
    return exports
  }
  const component = load(fileURLToPath(new URL('../src/components/UsageOverview.vue', import.meta.url)), js).default
  const state = scope.run(() => component.setup(props, { expose() {}, emit: (name: string, value: unknown) => events.push([name, value]) }))
  return { state, props, auth, events, unmount() { scope.stop() } }
}
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await vue.nextTick() }

test('真实概览切换服务后清除旧统计，迟到响应不能串到新服务', async () => {
  const first = deferred<UsageOverviewData>(), second = deferred<UsageOverviewData>(), paths: string[] = []
  const ui = mount(async path => { paths.push(path); return path.includes('/service-a/') ? first.promise : second.promise })
  await flush()
  assert.deepEqual(paths, ['/me/services/service-a/usage?period=current'])
  ui.props.serviceId = 'service-b'; await flush()
  assert.equal(ui.state.data.value, null)
  const result = overview(); result.service_id = 'service-b'; second.resolve(result); await flush()
  assert.equal(ui.state.data.value.service_id, 'service-b')
  first.resolve(overview()); await flush()
  assert.equal(ui.state.data.value.service_id, 'service-b')
  ui.unmount()
})

test('真实概览刷新去重并保留已采样图形，网络失败保旧且不改采样时间', async () => {
  const pending = deferred<UsageOverviewData>(), paths: string[] = []
  const ui = mount(async path => { paths.push(path); return paths.length > 2 ? pending.promise : { ...overview(), period: path.includes('7d') ? '7d' : 'current' } })
  await flush(); assert.equal(ui.state.percentage.value, 30)
  ui.state.period.value = '7d'; await flush()
  assert.equal(ui.state.data.value.period, '7d')
  assert.match(paths[1], /period=7d$/)
  const previous = ui.state.data.value, readAt = ui.state.reader.lastReadAt.value
  ui.props.refreshKey++; await flush()
  assert.equal(ui.state.data.value, previous)
  assert.equal(ui.state.percentage.value, 30)
  ui.props.refreshKey++; await flush()
  assert.equal(paths.length, 3)
  pending.reject(new api.ApiError(0, 'NETWORK_ERROR', '网络暂不可用')); await flush()
  assert.equal(ui.state.busy.value, false)
  assert.equal(ui.state.error.value, '网络暂不可用')
  assert.equal(ui.state.data.value, previous)
  assert.equal(ui.state.data.value.quality.collected_at, previous.quality.collected_at)
  assert.equal(ui.state.reader.lastReadAt.value, readAt)
  ui.unmount()
})

test('真实概览卸载后忽略迟到失败，未知与微量字节不会显示假零', async () => {
  const pending = deferred<UsageOverviewData>()
  const ui = mount(async () => pending.promise)
  assert.equal(ui.state.bytes(null), '暂无可靠统计')
  assert.equal(ui.state.bytes('1'), '< 0.01 GB')
  assert.equal(ui.state.bytes('0'), '0 GB')
  await flush()
  ui.unmount(); pending.reject(new api.ApiError(0, 'NETWORK_ERROR', '迟到失败')); await flush()
  assert.equal(ui.state.error.value, '')
  assert.equal(ui.state.data.value, null)
})

test('真实概览遇到401、403、404撤除缓存与图形数据，不套用网络保旧', async () => {
  for (const status of [401, 403, 404]) {
    let reads = 0
    const ui = mount(async () => { if (++reads === 1) return overview(); throw new api.ApiError(status, 'DENIED', '服务不可访问') })
    await flush(); assert.ok(ui.state.data.value)
    ui.props.refreshKey++; await flush()
    assert.equal(ui.state.data.value, null)
    assert.equal(ui.state.percentage.value, null)
    assert.equal(ui.state.reader.lastReadAt.value, null)
    assert.equal(ui.state.error.value, '服务不可访问')
    ui.unmount()
  }
})

test('真实概览切换身份清空旧数据且忽略旧身份迟到结果', async () => {
  const pending = deferred<UsageOverviewData>(), replacement = deferred<UsageOverviewData>()
  let reads = 0
  const ui = mount(async () => ++reads === 1 ? overview() : reads === 2 ? pending.promise : replacement.promise)
  await flush(); ui.props.refreshKey++; await flush()
  ui.auth.session.user.username = 'fixture-b'; await flush()
  assert.equal(ui.state.data.value, null)
  const next = overview(); next.summary.charged_bytes = '40000000000'
  replacement.resolve(next); await flush()
  pending.resolve(overview()); await flush()
  assert.equal(ui.state.data.value.summary.charged_bytes, '40000000000')
  ui.unmount()
})

test('受控概览只消费父快照并发送周期变化，不重复发请求或清空刷新内容', async () => {
  let reads = 0
  const ui = mount(async () => { reads++; return overview() }, true)
  ui.props.snapshot = overview(); ui.props.loading = true; await flush()
  assert.equal(reads, 0)
  assert.equal(ui.state.data.value, ui.props.snapshot)
  assert.equal(ui.state.busy.value, true)
  ui.state.period.value = '7d'; await flush()
  assert.deepEqual(ui.events, [['period-change', '7d']])
  ui.props.loading = false; ui.props.loadError = '网络暂不可用'; await flush()
  assert.ok(ui.state.data.value)
  assert.equal(ui.state.error.value, '网络暂不可用')
  ui.unmount()
})
