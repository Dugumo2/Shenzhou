import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import { boundedRequest } from '../src/billingPending.ts'
import type { CandidateDelivery } from '../src/delivery-types.ts'

function data(serviceId = 'service-a', clientId = 'windows', revision = 1, ready = true): CandidateDelivery {
  const url = '/api/v1/me/services/' + serviceId + '/delivery/' + clientId + '/resources/subscription'
  return { service_id: serviceId, service_revision: revision, client_id: clientId, mode: 'synthetic',
    runtime_acceptance: 'NOT TESTED', state: ready ? 'ready' : 'not_prepared', message: '合成演示，不可连接',
    download_url: ready ? url : null, resources: ready ? [{ key: 'subscription', label: '演示资源',
      download_url: url, filename: 'demo-subscription.txt', content_type: 'text/plain; charset=utf-8',
      bytes: 100, sha256: '0'.repeat(64) }] : [], policy_sha256: ready ? '1'.repeat(64) : null,
    updates_available: false, update_error: '', rule_source: 'current_candidate_custom_rules',
    unsupported_resources: ['未接入的基础规则来源'] }
}

function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); await vue.nextTick() }

// 编译真实SFC的setup和模板，仅替换网络、UI组件及生命周期宿主。
const descriptor = parse(readFileSync(new URL('../src/components/DeliveryResources.vue', import.meta.url), 'utf8')).descriptor
const compiled = compileScript(descriptor, { id: 'delivery-real-component' })
const transpile = (source: string) => ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const js = transpile(compiled.content)
const rendered = compileTemplate({ source: descriptor.template!.content, id: 'delivery-real-component',
  filename: 'DeliveryResources.vue', compilerOptions: { bindingMetadata: compiled.bindings } })
assert.equal(rendered.errors.length, 0)
const renderJs = transpile(rendered.code)
const require = createRequire(import.meta.url)

function mount(send: (path: string, method?: string, value?: unknown) => Promise<unknown>,
               { serviceId = 'service-a', clientId = 'windows', timeoutMs = 1000 } = {}) {
  const hooks: Array<() => void> = [], scope = vue.effectScope()
  const props = vue.reactive({ serviceId, clientId })
  const exports: Record<string, any> = {}, templateExports: Record<string, any> = {}
  const moduleRequire = (name: string) => name === 'vue'
    ? { ...vue, onBeforeUnmount: (fn: () => void) => hooks.push(fn), resolveComponent: (name: string) => name }
    : name === '../api' ? { ...api, request: send }
      : name === '../billingPending' ? { boundedRequest: <T>(operation: Promise<T>) => boundedRequest(operation, timeoutMs) }
        : require(name)
  new Function('require', 'exports', js)(moduleRequire, exports)
  new Function('require', 'exports', renderJs)(moduleRequire, templateExports)
  const state = scope.run(() => exports.default.setup(props, { expose: () => {} }))
  return { state, props,
    render() { return templateExports.render({}, [], props, vue.proxyRefs(state), {}, {}) },
    unmount() { hooks.forEach(fn => fn()); scope.stop() } }
}

function hasClick(node: any, handler: unknown): boolean {
  if (!node || typeof node !== 'object') return false
  if (node.props?.onClick === handler) return true
  const children = node.children
  if (Array.isArray(children)) return children.some(item => hasClick(item, handler))
  if (children?.default) return children.default().some((item: any) => hasClick(item, handler))
  return false
}

test('真实组件首次读取且未准备时不造链接，获取只发送软件、修订和操作键', async () => {
  const calls: { path: string; method: string; body?: any }[] = []
  const ui = mount(async (path, method = 'GET', body) => {
    calls.push({ path, method, body })
    return data('service-a', 'windows', 1, method === 'POST')
  })
  await flush()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].path, '/me/services/service-a/delivery?client_adapter=windows')
  assert.equal(calls[0].method, 'GET')
  assert.equal(calls[0].body, undefined)
  assert.equal(ui.state.delivery.value.download_url, null)
  assert.deepEqual(ui.state.resources.value, [])
  assert.equal(ui.state.canObtain.value, true)
  await ui.state.obtain()
  assert.equal(calls[1].path, '/me/services/service-a/delivery')
  assert.equal(calls[1].method, 'POST')
  assert.deepEqual(Object.keys(calls[1].body).sort(), ['client_adapter', 'expected_revision', 'idempotency_key'])
  assert.equal(calls[1].body.client_adapter, 'windows')
  assert.equal(calls[1].body.expected_revision, 1)
  assert.match(calls[1].body.idempotency_key, /^[a-f0-9-]{36}$/)
  assert.equal(ui.state.resources.value.length, 1)
  assert.equal(ui.state.pending.value, null)
  ui.unmount()
})

test('真实组件未知失败保留已有资源；重读状态后仍用同键重试', async () => {
  for (const [status, code] of [[0, 'NETWORK_ERROR'], [503, 'INVALID_RESPONSE'], [401, 'authentication_required'], [403, 'permission_denied']] as const) {
    const bodies: any[] = []
    const ui = mount(async (_, method = 'GET', body) => {
      if (method === 'GET') return data()
      bodies.push(body)
      if (bodies.length === 1) throw new api.ApiError(status, code, '获取结果未知')
      return data()
    })
    await flush()
    const existing = ui.state.delivery.value
    await ui.state.obtain()
    assert.equal(ui.state.delivery.value, existing)
    assert.equal(ui.state.resources.value.length, 1)
    assert.equal(ui.state.obtaining.value, false)
    assert.equal(ui.state.pending.value.key, bodies[0].idempotency_key)
    await ui.state.load()
    assert.equal(ui.state.pending.value.key, bodies[0].idempotency_key)
    await ui.state.obtain()
    assert.deepEqual(bodies[1], bodies[0])
    assert.equal(ui.state.pending.value, null)
    ui.unmount()
  }
})

test('真实组件版本冲突后禁用旧修订，模板提供重读按钮且新修订可再次获取', async () => {
  let reads = 0
  const bodies: any[] = []
  const ui = mount(async (_, method = 'GET', body) => {
    if (method === 'GET') return data('service-a', 'windows', ++reads)
    bodies.push(body)
    if (bodies.length === 1) throw new api.ApiError(409, 'revision_conflict', '服务版本已经改变')
    return data('service-a', 'windows', 2)
  })
  await flush(); await ui.state.obtain()
  assert.equal(ui.state.pending.value, null)
  assert.equal(ui.state.canObtain.value, false)
  assert.match(ui.state.error.value, /重新读取/)
  assert.equal(hasClick(ui.render(), ui.state.load), true)
  await ui.state.obtain(); assert.equal(bodies.length, 1)
  await ui.state.load()
  assert.equal(ui.state.delivery.value.service_revision, 2)
  assert.equal(ui.state.canObtain.value, true)
  await ui.state.obtain()
  assert.equal(bodies[1].expected_revision, 2)
  assert.notEqual(bodies[1].idempotency_key, bodies[0].idempotency_key)
  ui.unmount()
})

test('真实组件冲突后的状态重读失败保留资源并阻止旧版本提交，随后可重读恢复', async () => {
  let reads = 0, posts = 0
  const ui = mount(async (_, method = 'GET') => {
    if (method !== 'GET') { posts++; throw new api.ApiError(409, 'revision_conflict', '修订冲突') }
    if (++reads === 2) throw new api.ApiError(0, 'NETWORK_ERROR', '重读失败')
    return data('service-a', 'windows', reads)
  })
  await flush(); await ui.state.obtain(); await ui.state.load()
  assert.equal(ui.state.delivery.value.service_revision, 1)
  assert.equal(ui.state.resources.value.length, 1)
  assert.equal(ui.state.canObtain.value, false)
  assert.equal(hasClick(ui.render(), ui.state.load), true)
  await ui.state.obtain(); assert.equal(posts, 1)
  await ui.state.load()
  assert.equal(ui.state.delivery.value.service_revision, 3)
  assert.equal(ui.state.canObtain.value, true)
  ui.unmount()
})

test('真实组件获取有限等待、超时同键重试；原请求迟到不能覆盖重试结果', async () => {
  const late = deferred<CandidateDelivery>(), bodies: any[] = []
  const ui = mount(async (_, method = 'GET', body) => {
    if (method === 'GET') return data()
    bodies.push(body)
    return bodies.length === 1 ? late.promise : data('service-a', 'windows', 2)
  }, { timeoutMs: 5 })
  await flush(); await ui.state.obtain()
  assert.equal(ui.state.obtaining.value, false)
  assert.match(ui.state.error.value, /超时/)
  assert.equal(ui.state.pending.value.key, bodies[0].idempotency_key)
  await ui.state.obtain()
  assert.deepEqual(bodies[1], bodies[0])
  assert.equal(ui.state.delivery.value.service_revision, 2)
  late.resolve(data('service-a', 'windows', 99)); await flush()
  assert.equal(ui.state.delivery.value.service_revision, 2)
  assert.equal(ui.state.pending.value, null)
  assert.equal(ui.state.error.value, '')
  ui.unmount()
})

test('真实组件读取也有限等待，迟到旧读取不会补回状态', async () => {
  const late = deferred<CandidateDelivery>()
  let reads = 0
  const ui = mount(async () => ++reads === 1 ? late.promise : data('service-a', 'windows', 2), { timeoutMs: 5 })
  await new Promise(resolve => setTimeout(resolve, 20))
  assert.equal(ui.state.loading.value, false)
  assert.match(ui.state.error.value, /超时/)
  assert.equal(ui.state.delivery.value, null)
  assert.equal(ui.state.canObtain.value, false)
  await ui.state.load()
  late.resolve(data('service-a', 'windows', 99)); await flush()
  assert.equal(ui.state.delivery.value.service_revision, 2)
  ui.unmount()
})

test('真实组件切换软件清空旧请求与链接；旧获取迟到不能串到新软件', async () => {
  const late = deferred<CandidateDelivery>(), bodies: any[] = []
  const ui = mount(async (path, method = 'GET', body: any) => {
    if (method === 'GET') return data('service-a', path.includes('client_adapter=android') ? 'android' : 'windows')
    bodies.push(body)
    return bodies.length === 1 ? late.promise : data('service-a', body.client_adapter)
  })
  await flush()
  const first = ui.state.obtain(); await flush()
  const oldKey = bodies[0].idempotency_key
  ui.props.clientId = 'android'; await flush()
  assert.equal(ui.state.pending.value, null)
  assert.equal(ui.state.obtaining.value, false)
  assert.equal(ui.state.delivery.value.client_id, 'android')
  await ui.state.obtain()
  assert.notEqual(bodies[1].idempotency_key, oldKey)
  late.resolve(data()); await first
  assert.equal(ui.state.delivery.value.client_id, 'android')
  assert.ok(ui.state.resources.value[0].download_url.includes('/android/resources/'))
  ui.unmount()
})

test('真实组件卸载后迟到成功不改变旧状态，新服务不会复用旧操作键', async () => {
  const late = deferred<CandidateDelivery>(), bodies: any[] = []
  const first = mount(async (_, method = 'GET', body) => {
    if (method === 'GET') return data()
    bodies.push(body); return late.promise
  })
  await flush()
  const saving = first.state.obtain(); await flush()
  const original = first.state.delivery.value, oldKey = bodies[0].idempotency_key
  first.unmount()
  const second = mount(async (_, method = 'GET', body) => {
    if (method === 'POST') bodies.push(body)
    return data('service-b', 'android')
  }, { serviceId: 'service-b', clientId: 'android' })
  await flush(); await second.state.obtain()
  assert.notEqual(bodies[1].idempotency_key, oldKey)
  late.resolve(data('service-a', 'windows', 99)); await saving
  assert.equal(first.state.delivery.value, original)
  assert.equal(second.state.delivery.value.service_id, 'service-b')
  assert.equal(second.state.delivery.value.client_id, 'android')
  assert.equal(second.state.error.value, '')
  second.unmount()
})

test('真实组件同实例切换服务会清空数据，迟到旧状态与错误均不能覆盖', async () => {
  const old = deferred<CandidateDelivery>(), latest = deferred<CandidateDelivery>()
  const ui = mount(async path => path.includes('/service-a/') ? old.promise : latest.promise)
  ui.props.serviceId = 'service-b'
  assert.equal(ui.state.delivery.value, null)
  assert.equal(ui.state.pending.value, null)
  assert.equal(ui.state.canObtain.value, false)
  latest.resolve(data('service-b')); await flush()
  old.reject(new api.ApiError(0, 'NETWORK_ERROR', '旧服务失败')); await flush()
  assert.equal(ui.state.delivery.value.service_id, 'service-b')
  assert.equal(ui.state.error.value, '')
  ui.unmount()
})

test('真实组件服务被阻止时清除下载动作，不支持软件不发送获取请求', async () => {
  const ui = mount(async (_, method = 'GET') => {
    if (method === 'GET') return data()
    throw new api.ApiError(409, 'delivery_blocked', '服务已经停用')
  })
  await flush(); await ui.state.obtain()
  assert.equal(ui.state.delivery.value.state, 'blocked')
  assert.equal(ui.state.delivery.value.download_url, null)
  assert.deepEqual(ui.state.resources.value, [])
  assert.equal(ui.state.canObtain.value, false)
  ui.unmount()
  let requests = 0
  const unsupported = mount(async () => { requests++; return data() }, { clientId: 'router' })
  await flush(); await unsupported.state.obtain()
  assert.equal(requests, 0)
  assert.equal(unsupported.state.supported.value, false)
  assert.equal(unsupported.state.canObtain.value, false)
  unsupported.unmount()
})

test('真实组件只展示当前服务软件的精确相对资源URL', async () => {
  const value = data()
  const original = value.resources[0]
  for (const url of ['https://foreign.example.test/asset', 'javascript:alert(1)',
    '/api/v1/me/services/service-b/delivery/windows/resources/subscription',
    '/api/v1/me/services/service-a/delivery/android/resources/subscription',
    original.download_url + '?token=unexpected', original.download_url + '/../other']) {
    value.resources.push({ ...original, download_url: url })
  }
  const ui = mount(async () => value)
  await flush()
  assert.deepEqual(ui.state.resources.value, [original])
  ui.unmount()
})
