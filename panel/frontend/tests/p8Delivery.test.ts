import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import * as p8 from '../src/p8Delivery.ts'
import * as display from '../src/display.ts'
import { boundedRequest } from '../src/billingPending.ts'
import type { P8Delivery } from '../src/p8Delivery.ts'
import { useSnapshotRequest } from '../src/useSnapshotRequest.ts'
import { useSessionIdentity } from '../src/useSessionIdentity.ts'
import * as resourceSnapshot from '../src/resourceSnapshot.ts'

const require = createRequire(import.meta.url)
const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); await vue.nextTick() }
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
function payload(clientId = 'android', serviceId = 'service-a'): P8Delivery {
  return { service_id: serviceId, client_id: clientId, state: 'available', message: '资源格式已核对', runtime_acceptance: 'not_tested',
    resources: p8.p8ConnectionSteps(clientId).flatMap(step => step.keys).map(key => ({ key, label: key,
      download_url: 'https://subscription.example.test:2053/s/synthetic-only/' + key, verification: 'http_format_verified' })) }
}

// 执行真实 SFC，只替换请求、路由与生命周期宿主；所有地址都是不可连接的合成数据。
function mount(relative: string, initialProps: Record<string, unknown>, replacements: Record<string, unknown> = {}) {
  const descriptor = parse(readFileSync(new URL('../src/' + relative, import.meta.url), 'utf8')).descriptor
  const compiled = compileScript(descriptor, { id: 'p8-real-component' })
  const rendered = compileTemplate({ source: descriptor.template!.content, id: 'p8-real-component', filename: relative,
    compilerOptions: { bindingMetadata: compiled.bindings } })
  assert.equal(rendered.errors.length, 0)
  const transpile = (source: string) => ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const hooks: Array<() => void> = [], mounted: Array<() => void> = [], scope = vue.effectScope()
  const props = vue.reactive(initialProps), exports: Record<string, any> = {}, templateExports: Record<string, any> = {}
  const localRequire = (name: string): unknown => {
    if (name in replacements) return replacements[name]
    if (name === 'vue') return { ...vue, onMounted: (fn: () => void) => mounted.push(fn), onBeforeUnmount: (fn: () => void) => hooks.push(fn), resolveComponent: (name: string) => name }
    if (name === '../p8Delivery') return p8
    if (name === '../display') return display
    if (name === '../billingPending') return { boundedRequest }
    if (name === '../useSessionIdentity') return { useSessionIdentity }
    if (name === '../resourceSnapshot') return resourceSnapshot
    if (name === '../useSnapshotRequest') return { useSnapshotRequest: (options: any = {}) => useSnapshotRequest({ ...options, read: (path, signal) => (replacements['../api'] as typeof api).request(path, 'GET', undefined, signal) }) }
    if (name.endsWith('.vue')) return { default: name }
    return require(name)
  }
  new Function('require', 'exports', transpile(compiled.content))(localRequire, exports)
  new Function('require', 'exports', transpile(rendered.code))(localRequire, templateExports)
  const state = scope.run(() => exports.default.setup(props, { expose: () => {} }))
  mounted.forEach(fn => fn())
  return { props, state, render: () => templateExports.render({}, [], props, vue.proxyRefs(state), {}, {}),
    unmount() { hooks.forEach(fn => fn()); scope.stop() } }
}
function delivery(send: (path: string, method?: string) => Promise<P8Delivery>, clientId = 'android') {
  return mount('components/P8DeliveryResources.vue', { serviceId: 'service-a', clientId, refreshKey: 0 }, { '../api': { ...api, request: send } })
}
function nodes(node: any): any[] {
  if (!node || typeof node !== 'object') return []
  const children = Array.isArray(node.children) ? node.children : node.children?.default?.() || []
  return [node, ...children.flatMap(nodes)]
}
function links(node: any): string[] { return nodes(node).filter(item => typeof item.props?.href === 'string').map(item => item.props.href) }
function text(node: any): string { return nodes(node).filter(item => typeof item.children === 'string').map(item => item.children).join(' ') }

test('按软件读取既有资源且不创建授权，SFA只展示完整配置和应用步骤', async () => {
  const calls: { path: string; method?: string }[] = []
  const value = payload()
  value.resources.push({ ...value.resources[0], key: 'internal-policy' })
  const ui = delivery(async (path, method) => { calls.push({ path, method }); return value })
  await flush()
  assert.deepEqual(calls, [{ path: '/me/services/service-a/p8-delivery?client_adapter=android', method: undefined }])
  assert.deepEqual(ui.state.resources.value.map((item: any) => item.key), ['android'])
  assert.deepEqual(links(ui.render()), [value.resources[0].download_url])
  assert.match(text(ui.render()), /客户端导入、应用和实际连接尚未验收/)
  ui.state.activeStep.value = 1
  assert.deepEqual(links(ui.render()), [])
  ui.unmount()
})

test('v2rayNG按节点、名单、路由、应用分步，路由要求文件内容而非地址', async () => {
  const value = payload('v2rayng'), ui = delivery(async () => value, 'v2rayng')
  await flush()
  const expected = [['v2rayng'], ['v2rayng-geosite', 'v2rayng-geoip'], ['v2rayng-routes'], []]
  for (let index = 0; index < expected.length; index++) {
    ui.state.activeStep.value = index
    assert.deepEqual(links(ui.render()), expected[index].map(key => value.resources.find(item => item.key === key)!.download_url))
  }
  ui.state.activeStep.value = 2
  assert.match(text(ui.render()), /粘贴文件内容，不是下载地址/)
  ui.unmount()
})

test('Windows未核验或只提供素材包时不展示可导入链接', async () => {
  const value = payload('windows'); value.state = 'blocked'; value.message = 'Windows资源尚未核验'
  const ui = delivery(async () => value, 'windows')
  await flush()
  assert.deepEqual(links(ui.render()), [])
  assert.ok(nodes(ui.render()).some(item => item.props?.title === value.message))
  const materials = payload('windows')
  materials.resources[1].key = 'windows-rules'
  assert.deepEqual(p8.verifiedP8Resources(materials, 'service-a', 'windows'), [])
  ui.unmount()
})

test('响应归属、软件、资源完整性和HTTPS检查失败时不泄露下载动作', async () => {
  const wrongService = payload('android', 'other-service'), wrongClient = payload('v2rayng')
  for (const value of [wrongService, wrongClient]) {
    const ui = delivery(async () => value); await flush()
    assert.deepEqual(links(ui.render()), [])
    assert.match(ui.state.error.value, /不一致/)
    ui.unmount()
  }
  for (const url of ['javascript:alert(1)', 'http://example.test/a', '/api/session-only', 'https://user:pass@example.test/a', 'https://example.test/a#fragment']) {
    const value = payload(); value.resources[0].download_url = url
    assert.deepEqual(p8.verifiedP8Resources(value, 'service-a', 'android'), [])
  }
  const missing = payload('v2rayng'); missing.resources.pop()
  assert.deepEqual(p8.verifiedP8Resources(missing, 'service-a', 'v2rayng'), [])
  const duplicated = payload(); duplicated.resources.push(duplicated.resources[0])
  assert.deepEqual(p8.verifiedP8Resources(duplicated, 'service-a', 'android'), [])
  const unverified = payload(); (unverified.resources[0] as any).verification = 'not_tested'
  assert.deepEqual(p8.verifiedP8Resources(unverified, 'service-a', 'android'), [])
})

test('刷新开始立即清除链接；撤销或请求失败不保留旧下载动作', async () => {
  const next = deferred<P8Delivery>()
  let requests = 0
  const ui = delivery(async () => ++requests === 1 ? payload() : next.promise)
  await flush(); assert.equal(links(ui.render()).length, 1)
  const loading = ui.state.load()
  assert.deepEqual(links(ui.render()), [])
  next.reject(new api.ApiError(403, 'permission_denied', '服务无权访问'))
  await loading
  assert.deepEqual(links(ui.render()), [])
  assert.equal(ui.state.delivery.value, null)
  ui.unmount()
})

test('切换服务与软件及卸载时拒绝迟到响应', async () => {
  const old = deferred<P8Delivery>(), latest = deferred<P8Delivery>()
  const ui = delivery(async path => path.includes('/service-a/') ? old.promise : latest.promise)
  ui.props.serviceId = 'service-b'; ui.props.clientId = 'v2rayng'
  latest.resolve(payload('v2rayng', 'service-b')); await flush()
  old.resolve(payload()); await flush()
  assert.equal(ui.state.delivery.value.service_id, 'service-b')
  assert.equal(ui.state.delivery.value.client_id, 'v2rayng')
  assert.equal(ui.state.activeStep.value, 0)
  ui.unmount()
  assert.equal(ui.state.delivery.value, null)
  let requests = 0
  const unsupported = delivery(async () => { requests++; return payload() }, 'router')
  await flush(); assert.equal(requests, 0); assert.deepEqual(links(unsupported.render()), [])
  unsupported.unmount()
})

test('详情P8分支优先于演示；刷新失败与换账号均清除旧服务', async () => {
  const auth = vue.reactive({ session: { authenticated: true, user: { username: 'owner-a' }, environment: { is_demo: true } } })
  const route = vue.reactive({ params: { id: 'service-a' } })
  const service = { id: 'service-a', source_type: 'p8', delivery: { download_url: null }, clients: [] }
  let fail = false
  const ui = mount('pages/ServicePage.vue', {}, { '../auth': { auth }, 'vue-router': { useRoute: () => route }, '../api': { ...api, request: async (path: string) => {
    if (path === '/catalog/clients') return { items: [{ id: 'android', name: 'SFA', device: 'phone', os: 'Android' }] }
    if (fail) throw new api.ApiError(403, 'permission_denied', '无权访问')
    return service
  } } })
  await flush()
  ui.state.device.value = 'phone'; await flush(); ui.state.os.value = 'Android'; await flush(); ui.state.clientId.value = 'android'; await flush()
  const rendered = nodes(ui.render())
  assert.ok(rendered.some(item => item.type === '../components/P8DeliveryResources.vue'))
  assert.ok(!rendered.some(item => item.type === '../components/DeliveryResources.vue'))
  fail = true
  const reloading = ui.state.load()
  assert.equal(ui.state.service.value?.id, 'service-a')
  await reloading; assert.equal(ui.state.service.value, null)
  fail = false; await ui.state.load()
  auth.session.user.username = 'owner-b'; fail = true; await flush()
  assert.equal(ui.state.service.value, null)
  ui.unmount()
})

test('P8和旧服务均展示统一套餐期限，不沿用采购资源日期', () => {
  const ui = mount('components/ServiceMetrics.vue', { service: { source_type: 'p8', usage: { quality: 'unknown' }, quota_bytes: null, used_bytes: null,
    remaining_bytes: null, next_reset_at: null, expires_at: null, status_label: '待核验' }, lifecycleOnly: true })
  assert.match(text(ui.render()), /下次流量重置/)
  assert.match(text(ui.render()), /到期时间/)
  assert.doesNotMatch(text(ui.render()), /按资源分别计算/)
  ;(ui.props.service as any).source_type = 'membership'
  assert.match(text(ui.render()), /暂未设置/)
  ui.unmount()
})

test('旧会员地址别名加载后标题、用量和连接均使用同一正式服务编号', async () => {
  const alias = '11111111-1111-4111-8111-111111111111', canonical = '22222222-2222-4222-8222-222222222222'
  const response = deferred<unknown>(), calls: string[] = []
  const ui = mount('pages/ServicePage.vue', {}, {
    '../auth': { auth: vue.reactive({ session: { authenticated: true, user: { username: 'owner-a' }, environment: { is_demo: false } } }) },
    'vue-router': { useRoute: () => vue.reactive({ params: { id: alias } }) },
    '../api': { ...api, request: async (path: string) => {
      calls.push(path)
      return path === '/catalog/clients' ? { items: [{ id: 'android', name: 'SFA', device: 'phone', os: 'Android' }] } : response.promise
    } },
  })
  assert.match(text(ui.render()), /#11111111/)
  response.resolve({ id: canonical, source_type: 'p8', delivery: { download_url: null }, clients: [] })
  await flush()
  ui.state.device.value = 'phone'; await flush(); ui.state.os.value = 'Android'; await flush(); ui.state.clientId.value = 'android'; await flush()
  assert.ok(calls.includes('/me/services/' + alias))
  const rendered = ui.render()
  assert.match(text(rendered), /#22222222/)
  assert.doesNotMatch(text(rendered), /#11111111/)
  const consuming = nodes(rendered).filter(item => ['../components/UsageOverview.vue', '../components/P8DeliveryResources.vue'].includes(item.type))
  assert.equal(consuming.length, 3)
  assert.ok(consuming.every(item => item.props['service-id'] === canonical))
  ui.unmount()
})

test('用量接口初次失败保留已知套餐图数据，后续撤权同时清空父服务', async()=>{
  let status=0
  const service={id:'service-a',source_type:'p8',quota_bytes:'100000000000',used_bytes:null,remaining_bytes:null,quota_state:'applied',next_reset_at:null,expires_at:null,usage:{quality:'unknown',updated_at:null,message:''},delivery:{download_url:null},clients:[]}
  const ui=mount('pages/ServicePage.vue',{}, {
    '../auth':{auth:vue.reactive({session:{authenticated:true,user:{username:'owner-a'},environment:{is_demo:false}}})},
    'vue-router':{useRoute:()=>vue.reactive({params:{id:'service-a'}})},
    '../api':{...api,request:async(path:string)=>{
      if(path==='/catalog/clients')return {items:[]}
      if(path.includes('/usage?'))throw new api.ApiError(status,'READ_FAILED','读取失败')
      return service
    }}
  })
  await flush()
  assert.equal(ui.state.summarySnapshot.value.summary.quota_bytes,'100000000000')
  assert.equal(ui.state.summarySnapshot.value.summary.remaining_bytes,null)
  assert.equal(ui.state.service.value.id,'service-a')
  status=403;await ui.state.load();await flush()
  assert.equal(ui.state.service.value,null);assert.equal(ui.state.summarySnapshot.value,null)
  ui.unmount()
})
