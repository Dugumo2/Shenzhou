import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import * as display from '../src/display.ts'
import { boundedRequest } from '../src/billingPending.ts'

const descriptor = parse(readFileSync(new URL('../src/components/ObservedSite.vue', import.meta.url), 'utf8')).descriptor
const compiled = compileScript(descriptor, { id: 'observed-site' })
const js = ts.transpileModule(compiled.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await vue.nextTick() }
function fixture() {
  return { schema_version: 1, source_label: '合成核验资料', captured_at: null, assembled_at: '2026-10-04T20:00:00Z', units: [{ name: 'panel', status: 'active' }], accounts: { total: 1, active_admins: 1 },
    membership: { registered_quota_bytes: '1000000000000', expires_at: null, provisioning_state: 'pending_apply', usage_state: 'not_connected' },
    rules: [{ id: 1, action: 'direct', kind: 'domain', value: 'example.test', scope_domain: 'stream', enabled: true, revision: 1 }],
    resources: [{ key: 'android', label: 'Android配置', http_status: 200, format: 'json', status: 'verified', message: '仅格式核验' }, { key: 'windows-native', label: 'Windows原生路由', http_status: 503, format: '', status: 'blocked', message: '仍未交付' }], limitations: ['不是实时监控'] }
}
function mount(send: (path: string) => Promise<unknown>, timeout = 1000) {
  const hooks: Array<() => void> = [], mounted: Array<() => void> = [], events: unknown[][] = [], scope = vue.effectScope(), exports: Record<string, any> = {}
  const load = (name: string) => name === 'vue' ? { ...vue, onMounted: (fn: () => void) => mounted.push(fn), onBeforeUnmount: (fn: () => void) => hooks.push(fn) }
    : name === '../api' ? { ...api, request: send } : name === '../display' ? display : name === '../billingPending' ? { boundedRequest: (operation: Promise<unknown>) => boundedRequest(operation, timeout) } : undefined
  new Function('require', 'exports', js)(load, exports)
  const state = scope.run(() => exports.default.setup({}, { expose() {}, emit: (...event: unknown[]) => events.push(event) }))
  mounted.forEach(fn => fn())
  return { state, events, unmount() { hooks.forEach(fn => fn()); scope.stop() } }
}
test('只读快照读取、规则搜索和503异常分开呈现，不生成链接', async () => {
  const paths: string[] = [], ui = mount(async path => { paths.push(path); return fixture() })
  await flush()
  assert.deepEqual(paths, ['/admin/observed-site'])
  assert.deepEqual(ui.events, [['loaded', true]])
  assert.equal(ui.state.snapshot.value.membership.provisioning_state, 'pending_apply')
  assert.equal(ui.state.resourceFailures.value.length, 1)
  assert.equal(ui.state.resourceFailures.value[0].http_status, 503)
  ui.state.search.value = 'EXAMPLE'; assert.equal(ui.state.rules.value.length, 1)
  ui.state.search.value = '不存在'; assert.equal(ui.state.rules.value.length, 0)
  assert.match(descriptor.template!.content, /仅登记额度，尚未应用/)
  assert.match(descriptor.template!.content, /非实时监控/)
  assert.match(descriptor.template!.content, /快照整理时间/)
  assert.match(descriptor.template!.content, /时区待核/)
  assert.doesNotMatch(descriptor.template!.content, /formatDate\(snapshot.membership.expires_at\)/)
  assert.doesNotMatch(descriptor.template!.content, /:href|download_url/)
  ui.unmount()
})
test('404只提示未接入，失败隐藏旧快照且不泄露服务器路径', async () => {
  const notFound = mount(async () => { throw new api.ApiError(404, 'missing', '/private/hidden.json') })
  await flush(); assert.equal(notFound.state.message.value, '线上只读资料尚未接入。'); notFound.unmount()
  let fail = false
  const ui = mount(async () => { if (fail) throw new api.ApiError(403, 'denied', '/private/secret'); return fixture() })
  await flush(); fail = true
  const loading = ui.state.load(); assert.equal(ui.state.snapshot.value, null)
  await loading; assert.equal(ui.state.snapshot.value, null)
  assert.equal(ui.state.message.value, '暂时无法读取线上只读资料，请稍后重试。')
  assert.deepEqual(ui.events.at(-1), ['loaded', false]); ui.unmount()
})
test('超时有限等待，卸载后的迟到响应不得恢复快照', async () => {
  let resolve!: (value: unknown) => void
  const operation = new Promise(resolveValue => { resolve = resolveValue })
  const ui = mount(async () => operation, 5)
  await new Promise(resolveValue => setTimeout(resolveValue, 15))
  assert.equal(ui.state.busy.value, false); assert.equal(ui.state.snapshot.value, null)
  ui.unmount(); resolve(fixture()); await flush()
  assert.equal(ui.state.snapshot.value, null)
  assert.ok(!ui.events.some(event => event[1] === true))
})
