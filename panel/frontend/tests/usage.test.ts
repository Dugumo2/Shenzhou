import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import * as display from '../src/display.ts'
import * as usage from '../src/usage-types.ts'
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
function mount(send: (path: string) => Promise<UsageOverviewData>) {
  const hooks: Array<() => void> = [], scope = vue.effectScope()
  const props = vue.reactive({ serviceId: 'service-a', refreshKey: 0 })
  const exports: Record<string, any> = {}
  const moduleRequire = (name: string) => name === 'vue' ? { ...vue, onBeforeUnmount: (fn: () => void) => hooks.push(fn) }
    : name === '../api' ? { ...api, request: send } : name === '../display' ? display : name === '../usage-types' ? usage : name === './UsageDashboard.vue' ? {} : require(name)
  new Function('require', 'exports', js)(moduleRequire, exports)
  const state = scope.run(() => exports.default.setup(props, { expose: () => {} }))
  return { state, props, unmount() { hooks.forEach(fn => fn()); scope.stop() } }
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
  assert.deepEqual(paths, ['/me/services/service-a/usage?period=current'])
  ui.props.serviceId = 'service-b'; await flush()
  assert.equal(ui.state.data.value, null)
  const result = overview(); result.service_id = 'service-b'; second.resolve(result); await flush()
  assert.equal(ui.state.data.value.service_id, 'service-b')
  first.resolve(overview()); await flush()
  assert.equal(ui.state.data.value.service_id, 'service-b')
  ui.unmount()
})

test('真实概览切换时段和刷新会重读，失败不会继续展示旧可靠进度', async () => {
  const pending = deferred<UsageOverviewData>(), paths: string[] = []
  const ui = mount(async path => { paths.push(path); return paths.length > 2 ? pending.promise : { ...overview(), period: path.includes('7d') ? '7d' : 'current' } })
  await flush(); assert.equal(ui.state.percentage.value, 30)
  ui.state.period.value = '7d'; await flush()
  assert.equal(ui.state.data.value.period, '7d')
  assert.match(paths[1], /period=7d$/)
  ui.props.refreshKey++; await flush()
  assert.equal(ui.state.data.value, null)
  assert.equal(ui.state.percentage.value, null)
  pending.reject(new api.ApiError(0, 'NETWORK_ERROR', '网络暂不可用')); await flush()
  assert.equal(ui.state.busy.value, false)
  assert.equal(ui.state.error.value, '网络暂不可用')
  assert.equal(ui.state.data.value, null)
  ui.unmount()
})

test('真实概览卸载后忽略迟到失败，未知与微量字节不会显示假零', async () => {
  const pending = deferred<UsageOverviewData>()
  const ui = mount(async () => pending.promise)
  assert.equal(ui.state.bytes(null), '暂无可靠统计')
  assert.equal(ui.state.bytes('1'), '< 0.01 GB')
  assert.equal(ui.state.bytes('0'), '0 GB')
  ui.unmount(); pending.reject(new api.ApiError(0, 'NETWORK_ERROR', '迟到失败')); await flush()
  assert.equal(ui.state.error.value, '')
  assert.equal(ui.state.data.value, null)
})
