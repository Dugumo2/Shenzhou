import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import * as billing from '../src/billingPending.ts'
import * as pending from '../src/rulePending.ts'
import * as policies from '../src/rulePolicies.ts'
import * as sources from '../src/sourceImport.ts'

const require = createRequire(import.meta.url)
const transpile = (content: string) => ts.transpileModule(content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
function component(relative: string) {
  const descriptor = parse(readFileSync(new URL('../src/' + relative, import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: 'rule-section-nav', inlineTemplate: true })
  const exports: Record<string, any> = {}
  new Function('require', 'exports', transpile(script.content))(require, exports)
  return exports.default
}
const Nav = component('components/RuleSectionNav.vue')
async function render(props: Record<string, unknown>) {
  const app = vue.createSSRApp(Nav, props)
  app.component('RouterLink', vue.defineComponent({ props: ['to'], setup(value, { slots }) { return () => vue.h('a', { href: value.to }, slots.default?.()) } }))
  return renderToString(app)
}

test('正式只读显示三个短页签和可点击的同源管理入口', async () => {
  const html = await render({ active: 'sources', readOnly: true, production: true })
  assert.match(html, />自建规则</)
  assert.match(html, />规则来源</)
  assert.match(html, />规则方案</)
  assert.equal((html.match(/aria-current="page"/g) || []).length, 1)
  assert.match(html, /href="\/admin\/rules\/sources"[^>]*aria-current="page"/)
  assert.match(html, /href="\/manage\/client-routing\/"/)
  assert.match(html, /管理线上规则/)
  assert.match(html, /此页面暂不支持修改线上规则/)
  assert.doesNotMatch(html, /本地候选|导入、查看版本|组合、绑定与检查/)
})

test('本地只读与尚未确认能力不被误报为生产限制', async () => {
  const local = await render({ active: 'custom', readOnly: true, production: false })
  assert.match(local, /当前规则接口只读/)
  assert.doesNotMatch(local, /href="\/manage\/client-routing\/"|修改线上规则/)
  const pending = await render({ active: 'custom', production: true })
  assert.doesNotMatch(pending, /此页面暂不支持|href="\/manage\/client-routing\/"/)
})

test('发布和HTTPS限制只使用接口明确返回的能力', async () => {
  const blocked = await render({ active: 'policies', readOnly: true, production: true, publishAvailable: false, httpsAvailable: false })
  assert.match(blocked, /方案发布到订阅链接尚未接通/)
  assert.match(blocked, /HTTPS 来源获取暂未开放/)
  const unknown = await render({ active: 'policies', readOnly: false, production: false })
  assert.doesNotMatch(unknown, /方案发布到订阅链接尚未接通|HTTPS 来源获取暂未开放/)
})

const flush = async () => { for (let n = 0; n < 16; n++) await Promise.resolve(); await vue.nextTick() }
function mountPage(relative: string, readOnly: boolean) {
  const descriptor = parse(readFileSync(new URL('../src/pages/' + relative, import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: 'rules-page-wiring' })
  const template = compileTemplate({ source: descriptor.template!.content, filename: relative, id: 'rules-page-wiring', compilerOptions: { bindingMetadata: script.bindings } })
  assert.equal(template.errors.length, 0)
  const auth = vue.reactive({ ready: true, session: { authenticated: true, user: { username: 'fixture-' + relative, is_staff: true }, environment: { kind: 'production', is_demo: false } } })
  const starts: Array<() => unknown> = [], ends: Array<() => unknown> = [], calls: Array<{ path: string; method: string }> = []
  const exports: Record<string, any> = {}, rendered: Record<string, any> = {}, scope = vue.effectScope()
  const pageRequest = async (path: string, method = 'GET') => {
    calls.push({ path, method })
    return { read_only: readOnly, items: [], sources: [], custom: [], history: [], limitations: [], message: '这是本地数据库候选', candidate_revision: 'fixture', pagination: { pages: 0 }, production_publish_available: false, https_import_available: false }
  }
  const localRequire = (name: string): unknown => {
    if (name === 'vue') return { ...vue, onMounted: (fn: () => unknown) => starts.push(fn), onUnmounted: (fn: () => unknown) => ends.push(fn), resolveComponent: (name: string) => ({ name, render: () => null }) }
    if (name === '../auth') return { auth, hydrateSession: async () => {} }
    if (name === '../api') return { ...api, request: pageRequest }
    if (name === '../billingPending') return billing
    if (name === '../rulePending') return pending
    if (name === '../rulePolicies') return policies
    if (name === '../sourceImport') return sources
    if (name.endsWith('.vue')) return { default: name }
    return require(name)
  }
  new Function('require', 'exports', transpile(script.content))(localRequire, exports)
  new Function('require', 'exports', transpile(template.code))(localRequire, rendered)
  const state = scope.run(() => exports.default.setup({}, { expose() {} }))
  starts.forEach(fn => fn())
  return { state, calls, render: () => rendered.render({}, [], {}, vue.proxyRefs(state), {}, {}), unmount() { ends.forEach(fn => fn()); scope.stop() } }
}
function nodes(node: any): any[] { return node && typeof node === 'object' ? [node, ...(Array.isArray(node.children) ? node.children.flatMap(nodes) : [])] : [] }

test('三页把生产会话及真实只读信号接到统一导航，载入只执行查询', async () => {
  for (const [page, active] of [['RulesPage.vue', 'custom'], ['RuleSourcesPage.vue', 'sources'], ['RulePoliciesPage.vue', 'policies']]) {
    const ui = mountPage(page!, true)
    await flush()
    const rendered = nodes(ui.render())
    const nav = rendered.find(node => node.type === '../components/RuleSectionNav.vue')
    assert.equal(nav?.props.active, active)
    assert.equal(nav?.props['read-only'], true)
    assert.equal(nav?.props.production, true)
    assert.ok(ui.calls.length > 0)
    assert.ok(ui.calls.every(call => call.method === 'GET'))
    assert.ok(!rendered.some(node => node.type === 'el-alert' && node.props?.title === '这是本地数据库候选'))
    if (page === 'RulesPage.vue') { ui.state.openEditor(); assert.equal(ui.state.editOpen.value, false) }
    if (page === 'RulePoliciesPage.vue') { ui.state.begin(); assert.equal(ui.state.open.value, false) }
    ui.unmount()
  }
})
