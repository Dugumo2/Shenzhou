import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import * as display from '../src/display.ts'
import { effectiveFreshness } from '../src/inventoryStatus.ts'
import type { LinePathProjection, LinePathSegment, LineSegmentCapability } from '../src/inventoryTypes.ts'

const pagination = { page: 1, page_size: 25, total: 1, pages: 1, has_next: false, has_previous: false }
const line = (id = 'line-a') => ({ id, name: '已有线路', notes: '', revision: 1, enabled: true, ingresses: [{ id: '1', name: '真实入口', protocol: 'reality', protocol_label: 'REALITY', enabled: true, server: { id: 's1', name: '机器甲', enabled: true } }], ingress_count: 1, ingresses_truncated: false, egress: { id: '2', name: '外部出口', kind: 'upstream', kind_label: '上游', fail_closed: true, server: { id: 's1', name: '执行机器', enabled: true } }, endpoint_registration: 'enabled', verification: { state: 'not_tested', observed_at: null } })
const listing = (items = [line()]) => ({ items, pagination, read_only: true, limitations: [] })
const detail = (id = 'line-a') => ({ line: line(id), checks: { items: [], truncated: false }, capabilities: { edit: false, probe: false, publish: false }, limitations: [] })
const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); await vue.nextTick() }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(yes => { resolve = yes }); return { promise, resolve } }
const pageSource = readFileSync(new URL('../src/pages/AdminLinesPage.vue', import.meta.url), 'utf8')
function compile(source: string, inlineTemplate = false) {
  return ts.transpileModule(compileScript(parse(source).descriptor, { id: 'b7-lines', inlineTemplate }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
}
function mount(send: (path: string, method?: string, body?: unknown, signal?: AbortSignal) => Promise<unknown>) {
  const scope = vue.effectScope(), ends: Array<() => void> = [], exports: Record<string, any> = {}
  const route = vue.reactive({ query: {} as Record<string, string> })
  new Function('require', 'exports', compile(pageSource))((name: string) => {
    if (name === 'vue') return { ...vue, onUnmounted: (fn: () => void) => ends.push(fn) }
    if (name === 'vue-router') return { useRoute: () => route, useRouter: () => ({ replace: async (value: any) => { route.query = value.query } }) }
    if (name === '../api') return { ...api, request: send }
    if (name === '../inventoryStatus') return { effectiveFreshness, useInventoryClock: () => vue.ref(Date.now()) }
    if (name.endsWith('.vue')) return {}
    throw new Error('未声明的测试依赖：' + name)
  }, exports)
  const state = scope.run(() => exports.default.setup({}, { expose: () => {} }))
  return { state, route, ready: flush(), unmount() { ends.forEach(fn => fn()); scope.stop() } }
}

test('线路默认卡片；普通读取失败保留列表、选择、详情和原读取时间，恢复后更新', async () => {
  let fail = false
  const methods: Array<string | undefined> = []
  const ui = mount(async (path, method) => { methods.push(method); if (fail) throw new Error('读取暂不可用'); return path.includes('?') ? listing() : detail() })
  await ui.ready
  assert.equal(ui.state.viewMode.value, 'cards')
  await ui.state.showDetail(line())
  const previousRead = ui.state.lastReadAt.value
  fail = true
  await ui.state.load()
  assert.equal(ui.state.items.value.length, 1)
  assert.equal(ui.state.selected.value.id, 'line-a')
  assert.equal(ui.state.detail.value.line.id, 'line-a')
  assert.equal(ui.state.lastReadAt.value, previousRead)
  assert.ok(ui.state.error.value)
  fail = false
  await ui.state.load(); await flush()
  assert.equal(ui.state.error.value, '')
  assert.equal(ui.state.detail.value.line.id, 'line-a')
  assert.ok(methods.every(value => value === undefined))
  ui.unmount()
})

test('重复刷新复用请求，筛选变化隔离旧返回并保留显示方式', async () => {
  const requests: Array<ReturnType<typeof deferred<unknown>>> = []
  const ui = mount(async () => { const current = deferred<unknown>(); requests.push(current); return current.promise })
  await ui.ready
  const duplicate = ui.state.load()
  assert.equal(requests.length, 1)
  ui.state.viewMode.value = 'list'
  ui.route.query = { q: '另一个入口' }; await flush()
  assert.equal(requests.length, 2)
  requests[1].resolve(listing([line('new')])); await flush()
  requests[0].resolve(listing([line('old')])); await duplicate
  assert.equal(ui.state.items.value[0].id, 'new')
  assert.equal(ui.state.viewMode.value, 'list')
  ui.unmount()
})

test('同一详情刷新失败保旧；切换与关闭拒绝迟到响应', async () => {
  let fail = false
  const pending = deferred<unknown>()
  const ui = mount(async path => { if (path.includes('?')) return listing(); if (path.endsWith('/later')) return pending.promise; if (fail) throw new Error('详情失败'); return detail() })
  await ui.ready; await ui.state.showDetail(line())
  const previousReadAt = ui.state.detailReadAt.value
  assert.ok(previousReadAt)
  fail = true; await ui.state.showDetail(line())
  assert.equal(ui.state.detail.value.line.id, 'line-a')
  assert.equal(ui.state.detailReadAt.value, previousReadAt)
  assert.ok(ui.state.detailError.value)
  const late = ui.state.showDetail(line('later'))
  ui.state.closeDetail(); pending.resolve(detail('later')); await late
  assert.equal(ui.state.detail.value, null)
  assert.equal(ui.state.detailReadAt.value, null)
  ui.unmount()
})

test('详情抽屉内可统一刷新；15秒超时取消请求、保旧时间且拒绝迟到回填', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  let delayedMode = false, calls = 0, signal: AbortSignal | undefined
  const delayed = deferred<unknown>()
  const ui = mount(async (path, _method, _body, currentSignal) => {
    if (path.includes('?')) return listing()
    calls++; signal = currentSignal
    return delayedMode ? delayed.promise : detail()
  })
  await ui.ready; await ui.state.showDetail(line())
  const previousReadAt = ui.state.detailReadAt.value
  delayedMode = true
  const pending = ui.state.showDetail(line())
  await ui.state.showDetail(line())
  assert.equal(calls, 2)
  context.mock.timers.tick(14_999); await flush()
  assert.equal(ui.state.detailBusy.value, true)
  context.mock.timers.tick(1); await pending
  assert.equal(signal?.aborted, true)
  assert.equal(ui.state.detailBusy.value, false)
  assert.match(ui.state.detailError.value, /详情读取超时，已保留上次内容/)
  assert.equal(ui.state.detail.value.line.id, 'line-a')
  assert.equal(ui.state.detailReadAt.value, previousReadAt)
  delayed.resolve(detail('late')); await flush()
  assert.equal(ui.state.detail.value.line.id, 'line-a')
  delayedMode = false; await ui.state.showDetail(line())
  assert.equal(ui.state.detailError.value, '')
  const drawer = pageSource.split('<el-drawer')[1].split('</el-drawer>')[0]
  assert.match(drawer, /<RefreshControl[^>]+:loading="detailBusy"[^>]+:error="detailError"[^>]+:last-read-at="detailReadAt"/)
  assert.match(drawer, /@refresh="selected && showDetail\(selected\)"/)
  ui.unmount()
})

test('列表401/403清私有数据并取消详情，迟到详情不能回填，恢复后可重新读取', async () => {
  for (const status of [401, 403]) {
    let phase = 'ready'
    const delayed = deferred<unknown>(), signals: AbortSignal[] = []
    const ui = mount(async (path, _method, _body, signal) => {
      if (signal) signals.push(signal)
      if (path.includes('?')) {
        if (phase === 'denied') throw new api.ApiError(status, 'permission_denied', '无权读取线路。')
        return listing()
      }
      return phase === 'waiting' ? delayed.promise : detail()
    })
    await ui.ready; await ui.state.showDetail(line())
    phase = 'waiting'; const pendingDetail = ui.state.showDetail(line())
    const detailSignal = signals.at(-1)!
    phase = 'denied'; await ui.state.load()
    assert.equal(detailSignal.aborted, true)
    assert.deepEqual(ui.state.items.value, [])
    assert.equal(ui.state.detail.value, null)
    assert.equal(ui.state.selected.value, null)
    assert.equal(ui.state.lastReadAt.value, null)
    assert.equal(ui.state.pagination.value.total, 0)
    assert.equal(ui.state.busy.value, false)
    assert.equal(ui.state.detailBusy.value, false)
    delayed.resolve(detail()); await pendingDetail
    assert.equal(ui.state.detail.value, null)
    phase = 'ready'; await ui.state.load()
    assert.equal(ui.state.items.value.length, 1)
    assert.equal(ui.state.error.value, '')
    ui.unmount()
  }
})

test('详情401/403也清列表并取消读取，迟到列表不能恢复已失效内容', async () => {
  for (const status of [401, 403]) {
    let phase = 'ready', listSignal: AbortSignal | undefined
    const delayed = deferred<unknown>()
    const ui = mount(async (path, _method, _body, signal) => {
      if (path.includes('?')) {
        if (phase !== 'ready') { listSignal = signal; return delayed.promise }
        return listing()
      }
      if (phase === 'denied') throw new api.ApiError(status, 'permission_denied', '无权读取线路详情。')
      return detail()
    })
    await ui.ready; await ui.state.showDetail(line())
    phase = 'waiting'; const pendingList = ui.state.load()
    phase = 'denied'; await ui.state.showDetail(line())
    assert.equal(listSignal?.aborted, true)
    assert.equal(ui.state.detail.value, null)
    assert.equal(ui.state.selected.value, null)
    assert.match(ui.state.error.value, /无权读取/)
    delayed.resolve(listing()); await pendingList
    assert.deepEqual(ui.state.items.value, [])
    assert.equal(ui.state.lastReadAt.value, null)
    assert.equal(ui.state.busy.value, false)
    ui.unmount()
  }
})

test('网络与503失败仍保留列表和详情，不被权限清空分支误伤', async () => {
  for (const status of [0, 503]) {
    let fail = false
    const ui = mount(async path => { if (fail) throw new api.ApiError(status, 'unavailable', '暂不可读'); return path.includes('?') ? listing() : detail() })
    await ui.ready; await ui.state.showDetail(line())
    fail = true; await ui.state.load(); await ui.state.showDetail(line())
    assert.equal(ui.state.items.value.length, 1)
    assert.equal(ui.state.selected.value.id, 'line-a')
    assert.equal(ui.state.detail.value.line.id, 'line-a')
    assert.ok(ui.state.lastReadAt.value)
    ui.unmount()
  }
})

test('整链回执自动过期，路径变化优先，未知回执不显示通过', async () => {
  const ui = mount(async () => listing()); await ui.ready
  const row = line() as any
  row.verification = { state: 'fresh', result: 'pass', expires_at: '2020-01-01T00:00:00Z' }
  assert.equal(ui.state.verificationLabel(row), '检测已过期')
  row.verification.state = 'target_changed'
  assert.equal(ui.state.verificationLabel(row), '路径已变化')
  row.verification = { state: 'fresh', result: 'pass' }
  assert.equal(ui.state.verificationLabel(row), '未知')
  ui.unmount()
})

test('真实卡片渲染入口出口及检查时间，模板保留统一刷新与紧凑视图', async () => {
  const source = readFileSync(new URL('../src/components/LineInventoryCard.vue', import.meta.url), 'utf8'), exports: Record<string, any> = {}
  new Function('require', 'exports', compile(source, true))((name: string) => name === 'vue' ? vue : name === '../display' ? display : {}, exports)
  const app = vue.createSSRApp(exports.default, { line: line(), verification: '尚未检测' })
  app.component('ElButton', { setup: (_: any, { slots }: any) => () => vue.h('button', slots.default?.()) })
  app.component('ElTag', { setup: (_: any, { slots }: any) => () => vue.h('span', slots.default?.()) })
  const html = await renderToString(app)
  for (const text of ['真实入口', '外部出口', '尚无整链回执', '查看路径与详情', '登记关联不代表']) assert.ok(html.includes(text))
  assert.doesNotMatch(html, /目标版本|待接入/)
  assert.doesNotMatch(pageSource, /row\.target_version|在节点页查看/)
  assert.match(pageSource, /服务器详情的节点分区查看/)
  assert.match(pageSource, /detail\.line\.target_version/)
  assert.match(pageSource, /repeat\(auto-fill,/)
  assert.match(pageSource, /<RefreshControl/)
  assert.match(pageSource, /<LineInventoryCard/)
  assert.match(pageSource, /紧凑列表/)
  assert.doesNotMatch(pageSource, /v-else><div class="table-scroll"/)
})

const unknown = (): LineSegmentCapability => ({ state: 'unknown', source: '', observed_at: null, expires_at: null, freshness: 'unknown' })
const segment = (): LinePathSegment => ({ id: 'branch-1', from: { id: '1', name: '入口甲', kind: 'ingress' }, to: { id: '1', name: '外部出口', kind: 'egress' }, relation: 'registered', protocol: null, core: null, tcp: unknown(), udp: unknown(), dns: unknown(), authentication: 'unknown', failure_behavior: 'fail_closed_required' })
async function renderPath(segments: LinePathSegment[], kind: LinePathProjection['kind'] = 'registered_association') {
  const source = readFileSync(new URL('../src/components/LinePathDiagram.vue', import.meta.url), 'utf8'), exports: Record<string, any> = {}
  new Function('require', 'exports', compile(source, true))((name: string) => {
    if (name === 'vue') return vue
    if (name === '../display') return display
    if (name === '../inventoryStatus') return { effectiveFreshness, useInventoryClock: () => vue.ref(Date.parse('2026-10-17T00:00:00Z')) }
    throw new Error('未声明的图形测试依赖：' + name)
  }, exports)
  return renderToString(vue.createSSRApp(exports.default, { path: { kind, source: 'inventory_registration', observed_at: null, segments, warnings: [] } }))
}

test('真实路径图保未知中间段和核心，TCP通过不能提升UDP或DNS', async () => {
  const row = segment()
  row.tcp = { state: 'pass', source: 'fixed-local-test', observed_at: '2026-10-16T23:59:00Z', expires_at: '2026-10-17T00:01:00Z', freshness: 'fresh' }
  const html = await renderPath([row])
  assert.match(html, /<svg/)
  assert.match(html, /中间跳段未核实/)
  assert.match(html, /未知；不按同机或名称推断/)
  assert.match(html, /TCP<\/strong><span>检查通过 · 有效期内/)
  assert.match(html, /UDP<\/strong><span>未知 \/ 未验证/)
  assert.match(html, /DNS<\/strong><span>未知 \/ 未验证/)
  assert.match(html, /已登记失败关闭要求，执行未验证/)
  assert.doesNotMatch(html, /路径关系出现环路/)
})

test('混合路径逐段保失败和过期，环路不能显示成可用；单节点不画假连线', async () => {
  const first = segment(), second = segment()
  first.tcp = { state: 'pass', source: 'old-check', observed_at: '2026-10-16T00:00:00Z', expires_at: '2026-10-16T00:01:00Z', freshness: 'fresh' }
  second.id = 'return'; second.from = first.to; second.to = first.from
  second.udp = { state: 'fail', source: 'check', observed_at: '2026-10-16T23:59:00Z', expires_at: '2026-10-17T00:01:00Z', freshness: 'fresh' }
  const html = await renderPath([first, second], 'observed_path')
  assert.match(html, /检查通过 · 证据已过期/)
  assert.match(html, /检查失败 · 有效期内/)
  assert.match(html, /路径关系出现环路，不能视作可用路径/)
  const single = await renderPath([first], 'single_node')
  assert.match(single, /这是单节点资源/)
  assert.doesNotMatch(single, /<svg/)
  assert.doesNotMatch(single, /path-connection/)
})

test('配置路径展示实际双核心方向与证据，能力未验证；最终出口卡保留旧中间段', async () => {
  const first = segment(), hop = segment(), final = segment()
  const sb = { id: 'sb', name: '实际 sing-box', kind: 'core' as const }
  const xr = { id: 'xr', name: '实际 Xray', kind: 'core' as const }
  first.id = 'sb-out'; first.from = sb; first.to = { id: '1', name: '原 SOCKS 中转', kind: 'egress' }
  hop.id = 'loopback'; hop.from = first.to; hop.to = xr
  final.id = 'external'; final.from = xr; final.to = { id: '3', name: '真实外部 SS2022', kind: 'egress' }
  for (const row of [first, hop, final]) {
    row.relation = 'observed'; row.evidence_source = 'b1-live-evidence'; row.evidence_observed_at = '2026-10-17T12:20:05Z'
  }
  first.core = hop.core = { id: 'sb', name: sb.name, core_type: 'sing-box' }
  final.core = { id: 'xr', name: xr.name, core_type: 'xray' }
  first.protocol = hop.protocol = 'socks'; final.protocol = 'ss2022'
  const html = await renderPath([first, hop, final], 'observed_path')
  for (const text of ['已观测配置路径', '实际 sing-box', '原 SOCKS 中转', '实际 Xray', '真实外部 SS2022', 'b1-live-evidence']) assert.ok(html.includes(text))
  assert.equal((html.match(/未知 \/ 未验证/g) || []).length, 9)
  assert.doesNotMatch(html, /<span>检查通过/)
  const row: any = line()
  row.egress.name = '原 SOCKS 中转'
  row.target_version = null
  row.path = { kind: 'observed_path', source: 'b1-live-evidence', observed_at: '2026-10-17T12:20:05Z', segments: [], warnings: [],
    terminal_egress: { ...row.egress, id: '3', name: '真实外部 SS2022', protocol: 'ss2022', ownership: 'external' } }
  const source = readFileSync(new URL('../src/components/LineInventoryCard.vue', import.meta.url), 'utf8'), exports: Record<string, any> = {}
  new Function('require', 'exports', compile(source, true))((name: string) => name === 'vue' ? vue : name === '../display' ? display : {}, exports)
  const app = vue.createSSRApp(exports.default, { line: row, verification: '尚未检测' })
  app.component('ElButton', { setup: (_: any, { slots }: any) => () => vue.h('button', slots.default?.()) })
  app.component('ElTag', { setup: (_: any, { slots }: any) => () => vue.h('span', slots.default?.()) })
  const card = await renderToString(app)
  assert.match(card, /外部出口资源/)
  assert.match(card, /真实外部 SS2022/)
  assert.match(card, /经 原 SOCKS 中转 · 已有中间段/)
  assert.match(card, /尚无整链回执/)
})
