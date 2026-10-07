import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'
import type { Service } from '../src/types.ts'
import type { ProviderUsage, ResourceUsageMeter } from '../src/providerUsage.ts'

const require = createRequire(import.meta.url)
const root = fileURLToPath(new URL('../src/', import.meta.url))
function meter(changes: Partial<ResourceUsageMeter> = {}): ResourceUsageMeter {
  return { id: 'home', label: 'HOME', scope: 'external_node', source_kind: 'estimate', quality: 'current',
    quota_bytes: '400000000000', used_bytes: '73000000000', remaining_bytes: '327000000000', upload_bytes: '23000000000', download_bytes: '50000000000',
    observed_at: '2026-10-07T01:00:00Z', expires_at: '2099-10-07T02:00:00Z',
    cycle: { kind: 'fixed_days', starts_at: '2026-09-20T00:00:00+08:00', ends_at: '2026-10-20T00:00:00+08:00', next_reset_at: '2026-10-20T00:00:00+08:00' },
    expires_on: '2027-01-19', alerts: [], details: {}, ...changes }
}
function provider(): ProviderUsage {
  return { schema_version: 2, generated_at: '2026-10-07T01:00:00Z', meters: [meter(), meter({ id: 'bwg', label: 'BWG', scope: 'server', source_kind: 'official', quota_bytes: '2000000000000', used_bytes: '120000000000', remaining_bytes: '1880000000000', upload_bytes: null, download_bytes: null, expires_on: null })] }
}
function service(changes: Partial<Service> = {}): Service {
  return { id: 'same-public-service', name: '神舟云', source_type: 'p8', provider_usage: provider(), quota_bytes: null, used_bytes: null, raw_bytes: null,
    remaining_bytes: null, next_reset_at: null, expires_at: null, state: 'verification_required', enabled: true, status_label: '订阅已接入',
    usage: { quality: 'unknown', updated_at: null, message: '暂无可靠统计，当前用量和剩余待核算。' }, delivery: { state: 'verification_required', message: '', download_url: null },
    user: { username: 'fixture-admin' }, actions: { billing: false, quota: false, renew: false, grants: false, enable: false, reset: false }, ...changes }
}

// 真实管理页面与真实资源组件；只替换HTTP、Session和Element宿主，不复制业务模板。
async function renderPage(page: string, services: Service[], openUsage = false) {
  const cache = new Map<string, any>(), requests: string[] = []
  const target = resolve(root, page === 'AdminResourceUsage.vue' ? 'components' : 'pages', page)
  const session = { authenticated: true, user: { username: 'fixture-admin', is_staff: true }, csrf_token: 'fixture-session' }
  const pagination = { page: 1, page_size: 25, total: services.length, pages: 1, has_next: false, has_previous: false }
  const data = page === 'AdminResourceUsage.vue' ? {items:services.filter(s=>s.provider_usage).map(s=>({service_id:s.id,provider_usage:s.provider_usage}))} : page === 'AdminServicesPage.vue' ? { items: services, pagination } : { user: { id: 'fixture-admin', username: 'fixture-admin', is_active: true, is_staff: true, service_count: services.length, mapping_required: false }, services }
  function load(filename: string): any {
    if (cache.has(filename)) return cache.get(filename)
    if (filename.endsWith('BillingDrawer.vue')) return { default: { render: () => null } }
    const source = readFileSync(filename, 'utf8')
    let content = source
    if (filename.endsWith('.vue')) {
      const descriptor = parse(source).descriptor
      const script = compileScript(descriptor, { id: filename, inlineTemplate: filename !== target })
      content = script.content
      if (filename === target) content += '\n' + compileTemplate({ id: filename, source: descriptor.template!.content, filename, compilerOptions: { bindingMetadata: script.bindings } }).code
    }
    const code = ts.transpileModule(content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
    const exports: Record<string, any> = {}
    cache.set(filename, exports)
    new Function('require', 'exports', code)((name: string) => {
      if (name === 'vue') return vue
      if (name === 'vue-router') return { useRoute: () => ({ params: { id: 'fixture-admin' }, query: {} }) }
      if (/\/auth(?:\.ts)?$/.test(name)) return { auth: vue.reactive({ session }) }
      if (/\/api(?:\.ts)?$/.test(name)) return { request: async (path: string) => { requests.push(path); return data }, errorMessage: (error: Error) => error.message }
      if (!name.startsWith('.')) return require(name)
      return load(resolve(dirname(filename), /\.(vue|ts)$/.test(name) ? name : name + '.ts'))
    }, exports)
    if (filename === target) exports.default.render = exports.render
    return exports
  }
  const component = load(target).default
  const wrapper = { ...component, async setup(props: any, context: any) {
    const state = component.setup(props, context)
    for (let i = 0; i < 30; i++) await Promise.resolve()
    return state
  } }
  const app = vue.createSSRApp(wrapper)
  for (const name of ['el-button', 'el-alert', 'el-skeleton', 'el-tag', 'el-select', 'el-option', 'el-input', 'el-pagination', 'RouterLink']) app.component(name, { setup(_props: unknown, { slots }: any) { return () => vue.h('div', slots.default?.()) } })
  app.component('ElTable', { props: ['data'], setup(props: any, { slots }: any) { vue.provide('rows', props.data); return () => vue.h('div', slots.default?.()) } })
  app.component('ElTableColumn', { props: ['label'], setup(props: any, { slots }: any) { const rows = vue.inject<Service[]>('rows', []); return () => vue.h('section', { 'data-column': props.label }, [vue.h('h3', props.label), ...rows.map(row => vue.h('div', slots.default?.({ row })))]) } })
  app.component('ElDrawer', { props: ['modelValue'], setup(props: any, { slots }: any) { return () => props.modelValue ? vue.h('aside', { 'data-drawer': 'resources' }, slots.default?.()) : null } })
  app.directive('loading', {})
  return { html: await renderToString(app), requests }
}

test('管理订阅真实模板固定七列，P8和权益共用套餐摘要而不使用采购数字',async()=>{
  const personal=service({quota_bytes:'100000000000',used_bytes:'30000000000',remaining_bytes:'70000000000',expires_at:'2027-01-19T00:00:00+08:00',actions:{billing:true,quota:false,renew:false,grants:false,enable:false,reset:false}})
  const {html,requests}=await renderPage('AdminServicesPage.vue',[personal])
  assert.equal((html.match(/data-column=/g)||[]).length,7)
  for(const text of ['100 GB','30 GB','70 GB','2027','重置时间'])assert.ok(html.includes(text),text)
  assert.doesNotMatch(html,/各资源独立|按资源查看|HOME|BWG|>资源用量</)
  assert.equal(requests.length,1)
})

test('独立管理员资源区域保留两份采购圆环、原始上下行与各自日期',async()=>{
  const {html,requests}=await renderPage('AdminResourceUsage.vue',[service()])
  assert.deepEqual(requests,['/admin/resource-usage'])
  assert.equal((html.match(/data-chart="quota-gauge"/g)||[]).length,2)
  assert.equal((html.match(/data-chart="transfer-composition"/g)||[]).length,1)
  for(const text of ['资源采购用量','HOME','BWG','73 GB','120 GB','327 GB','1,880 GB','2027-01-19'])assert.ok(html.includes(text),text)
  assert.match(readFileSync(resolve(root,'pages/AdminOverviewPage.vue'),'utf8'),/<AdminResourceUsage \/>/)
})

test('管理员用户服务实际入口保留统一套餐字段，采购数据不占套餐位置',async()=>{
  const {html,requests}=await renderPage('AdminUserPage.vue',[service({quota_bytes:'100000000000',used_bytes:'30000000000',remaining_bytes:'70000000000'})])
  for(const text of ['本期总额度','100 GB','30 GB','70 GB','下次重置时间','到期时间'])assert.ok(html.includes(text),text)
  assert.doesNotMatch(html,/HOME|BWG|data-usage-scope="resources"/)
  assert.deepEqual(requests,['/admin/users/fixture-admin'])
})

test('无采购统计权限时不生成资源数字，订阅与用户页仍保留套餐字段',async()=>{
  const without=service({provider_usage:undefined})
  for(const page of ['AdminServicesPage.vue','AdminUserPage.vue','AdminResourceUsage.vue']){
    const {html}=await renderPage(page,[without]);assert.doesNotMatch(html,/HOME|BWG|73 GB|120 GB|data-chart=/)
  }
})

test('采购快照失败由独立管理区域显示，空来源不造0或百分比',async()=>{
  const unavailable=service({provider_usage:{schema_version:2,generated_at:null,meters:[],error:{code:'usage_unavailable',message:'统计暂不可用，请稍后更新。'}}})
  const {html}=await renderPage('AdminResourceUsage.vue',[unavailable])
  assert.match(html,/统计暂不可用，请稍后更新/)
  assert.doesNotMatch(html,/data-chart=|0 GB/)
})
