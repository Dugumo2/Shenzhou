import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as display from '../src/display.ts'

// 执行真实页面脚本，只提供假请求和响应式路由，不访问服务器。
function mount(page: string, route: any, request: (path: string) => Promise<unknown>) {
  const replacements: any[] = [], hooks: Array<() => void> = []
  const descriptor = parse(readFileSync(new URL('../src/pages/' + page, import.meta.url), 'utf8')).descriptor
  const script = compileScript(descriptor, { id: 'service-projection' })
  const code = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports: Record<string, any> = {}, scope = vue.effectScope()
  new Function('require', 'exports', code)((name: string) => {
    if (name === 'vue') return { ...vue, onMounted: (fn: () => void) => fn(), onUnmounted: (fn: () => void) => hooks.push(fn) }
    if (name === 'vue-router') return { useRoute: () => route, useRouter: () => ({ replace: async (value: any) => { replacements.push(value); route.query = value.query } }) }
    if (name === '../api') return { request, errorMessage: () => '假请求失败' }
    if (name === '../display') return display
    if (name.endsWith('.vue')) return { default: name }
    throw new Error('未声明测试依赖')
  }, exports)
  const state = scope.run(() => exports.default.setup({}, { expose() {} }))
  return { state, replacements, unmount() { hooks.forEach(fn => fn()); scope.stop() } }
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
  const ui = mount('AdminUserPage.vue', route, async () => ({ user: { username: 'fixture' }, services: [] }))
  await flush()
  ui.state.editBilling({ id: 'p8-fixture', source_type: 'p8', actions: { billing: false } })
  assert.equal(ui.state.selected.value, null)
  ui.state.editBilling({ id: 'entitlement-fixture', source_type: 'entitlement', actions: { billing: true } })
  assert.equal(ui.state.selected.value.id, 'entitlement-fixture')
  ui.unmount()
})
