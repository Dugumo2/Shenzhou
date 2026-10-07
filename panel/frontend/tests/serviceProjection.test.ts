import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as display from '../src/display.ts'
import { useSnapshotRequest } from '../src/useSnapshotRequest.ts'
import * as resourceSnapshot from '../src/resourceSnapshot.ts'
import { useSessionIdentity } from '../src/useSessionIdentity.ts'
import { ApiError } from '../src/api.ts'

// 执行真实页面脚本，只提供假请求和响应式路由，不访问服务器。
function mount(page: string, route: any, request: (path: string) => Promise<unknown>) {
  const replacements: any[] = [], hooks: Array<() => void> = []
  const auth = vue.reactive({ session: { authenticated: true, user: { username: 'admin', is_staff: true }, csrf_token: 'fixture-session' } })
  const descriptor = parse(readFileSync(new URL('../src/pages/' + page, import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: 'service-projection' })
  const code = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports: Record<string, any> = {}, scope = vue.effectScope()
  new Function('require', 'exports', code)((name: string) => {
    if (name === 'vue') return { ...vue, onMounted: (fn: () => void) => fn(), onUnmounted: (fn: () => void) => hooks.push(fn) }
    if (name === 'vue-router') return { useRoute: () => route, useRouter: () => ({ replace: async (value: any) => { replacements.push(value); route.query = value.query } }) }
    if (name === '../api') return { request, errorMessage: () => '假请求失败' }
    if (name === '../display') return display
    if (name === '../auth') return { auth }
    if (name === '../useSessionIdentity') return { useSessionIdentity }
    if (name === '../resourceSnapshot') return resourceSnapshot
    if (name === '../useSnapshotRequest') return { useSnapshotRequest: (options: any = {}) => useSnapshotRequest({ ...options, read: request }) }
    if (name.endsWith('.vue')) return { default: name }
    throw new Error('未声明测试依赖')
  }, exports)
  const state = scope.run(() => exports.default.setup({}, { expose() {} }))
  return { state, replacements, auth, unmount() { hooks.forEach(fn => fn()); scope.stop() } }
}
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await vue.nextTick() }

test('无服务筛选从路由恢复，切换筛选回第一页并保存到地址', async () => {
  const route = vue.reactive({ query: { service: 'none', page: '3', q: 'owner' }, params: {} })
  const calls: string[] = []
  const ui = mount('AdminUsersPage.vue', route, async path => { calls.push(path); return { items: [], pagination: { page: 3, total: 0 } } })
  await flush()
  assert.match(calls[0], /service=none/)
  assert.match(calls[0], /page=3/)
  ui.state.service.value = 'with'
  ui.state.search()
  await flush()
  assert.equal(ui.replacements[0].query.service, 'with')
  assert.equal(ui.replacements[0].query.page, undefined)
  assert.match(calls.at(-1)!, /service=with/)
  assert.match(calls.at(-1)!, /page=1/)
  ui.unmount()
})

test('用户详情尊重来源操作能力，P8不能打开账期编辑', async () => {
  const route = vue.reactive({ query: {}, params: { id: 'fixture-owner' } })
  const services = [{ id: 'p8-fixture', source_type: 'p8', actions: { billing: false } }, { id: 'entitlement-fixture', source_type: 'entitlement', actions: { billing: true } }]
  const ui = mount('AdminUserPage.vue', route, async () => ({ user: { username: 'fixture' }, services }))
  await flush()
  ui.state.editBilling({ id: 'p8-fixture', source_type: 'p8', actions: { billing: false } })
  assert.equal(ui.state.selected.value, null)
  ui.state.editBilling({ id: 'entitlement-fixture', source_type: 'entitlement', actions: { billing: true } })
  assert.equal(ui.state.selected.value.id, 'entitlement-fixture')
  ui.unmount()
})

function listing() {
  return { items: [{ id: 'p8-fixture', source_type: 'p8', provider_usage: { schema_version: 2, generated_at: '2026-10-07T05:00:00Z', meters: [{ id: 'home', used_bytes: '300', observed_at: '2026-10-07T04:59:00Z' }] }, actions: { billing: false } }], pagination: { page: 1, page_size: 25, total: 1, pages: 1, has_next: false, has_previous: false } }
}

test('订阅页同次刷新去重、保留真实资源抽屉，网络失败不替换来源时间', async () => {
  let reads = 0, reject: ((reason: unknown) => void) | undefined
  const data = listing()
  const ui = mount('AdminServicesPage.vue', vue.reactive({ query: {}, params: {} }), async () => {
    reads++
    if (reads === 1) return data
    return new Promise((_resolve, fail) => { reject = fail })
  })
  await flush()
  ui.state.viewUsage(ui.state.items.value[0])
  assert.deepEqual(ui.state.usageService.value.provider_usage, data.items[0].provider_usage)
  const first = ui.state.load(), second = ui.state.load()
  assert.equal(first, second)
  await flush()
  assert.equal(reads, 2)
  assert.equal(ui.state.initialLoading.value, false)
  assert.equal(ui.state.usageService.value.id, 'p8-fixture')
  reject!(new ApiError(0, 'NETWORK_ERROR', '网络失败'))
  await first; await flush()
  assert.equal(ui.state.items.value.length, 1)
  assert.equal(ui.state.usageService.value.provider_usage.meters[0].observed_at, '2026-10-07T04:59:00Z')
  ui.unmount()
})

test('订阅页401/403/404会清空列表和资源抽屉，不保留撤权后的副本', async () => {
  for (const status of [401, 403, 404]) {
    let reads = 0
    const ui = mount('AdminServicesPage.vue', vue.reactive({ query: {}, params: {} }), async () => {
      if (++reads === 1) return listing()
      throw new ApiError(status, 'DENIED', '无权访问')
    })
    await flush(); ui.state.viewUsage(ui.state.items.value[0])
    await ui.state.load(); await flush()
    assert.deepEqual(ui.state.items.value, [])
    assert.equal(ui.state.usageService.value, null)
    assert.equal(ui.state.usageId.value, null)
    ui.unmount()
  }
})

test('订阅页角色撤销同步清空，旧请求迟到不恢复来源数据', async () => {
  let reads = 0, resolveLate: ((value: unknown) => void) | undefined
  const ui = mount('AdminServicesPage.vue', vue.reactive({ query: {}, params: {} }), async () => {
    if (++reads === 1) return listing()
    return new Promise(resolve => { resolveLate = resolve })
  })
  await flush(); ui.state.viewUsage(ui.state.items.value[0]); const late = ui.state.load(); await flush()
  ui.auth.session.user.is_staff = false
  assert.deepEqual(ui.state.items.value, [])
  assert.equal(ui.state.usageService.value, null)
  resolveLate!(listing()); await late; await flush()
  assert.deepEqual(ui.state.items.value, [])
  assert.equal(reads, 2)
  ui.unmount()
})

test('管理员用户详情切换目标和会话立即清空账期抽屉，并拒绝迟到响应', async () => {
  const route = vue.reactive({ query: {}, params: { id: 'one' } })
  let calls = 0, resolveLate: ((value: unknown) => void) | undefined
  const service = { id: 'entitlement-fixture', source_type: 'entitlement', actions: { billing: true } }
  const ui = mount('AdminUserPage.vue', route, async () => {
    if (++calls === 1) return { user: { username: 'one' }, services: [service] }
    return new Promise(resolve => { resolveLate = resolve })
  })
  await flush(); ui.state.editBilling(service)
  assert.equal(ui.state.selected.value.id, service.id)
  route.params.id = 'two'
  assert.equal(ui.state.detail.value, null)
  assert.equal(ui.state.selected.value, null)
  await flush()
  const staleResolve = resolveLate!
  ui.auth.session.csrf_token = 'rotated-fixture-session'
  assert.equal(ui.state.detail.value, null)
  await flush()
  staleResolve({ user: { username: 'old-session' }, services: [service] })
  await flush()
  assert.equal(ui.state.detail.value, null)
  resolveLate!({ user: { username: 'new-session' }, services: [] })
  await flush()
  assert.equal(ui.state.detail.value.user.username, 'new-session')
  assert.equal(ui.state.selected.value, null)
  ui.unmount()
})
