import { readFileSync } from 'node:fs'
import { parse, compileScript, compileStyle } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import * as display from '../src/display.ts'

export const descriptor = parse(readFileSync(new URL('../src/components/ResourcePlanEditor.vue', import.meta.url), 'utf8')).descriptor
const refreshDescriptor = parse(readFileSync(new URL('../src/components/RefreshControl.vue', import.meta.url), 'utf8')).descriptor
export const css = [...descriptor.styles, ...refreshDescriptor.styles].map(style => compileStyle({ source: style.content, filename: 'ResourcePlanEditor.vue', id: 'purchase-editor', scoped: false }).code).join('\n')
export const refreshCode = ts.transpileModule(compileScript(refreshDescriptor, { id: 'purchase-refresh', inlineTemplate: true }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
export const displayCode = ts.transpileModule(readFileSync(new URL('../src/display.ts', import.meta.url), 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
function refreshModule() { const exports: any = {}; new Function('require', 'exports', refreshCode)((name: string) => name === 'vue' ? vue : display, exports); return exports }
export function compiled(inlineTemplate = false) {
  return ts.transpileModule(compileScript(descriptor, { id: 'purchase-editor', inlineTemplate }).content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
}
export function plan(source = 'residential'): any {
  const result: any = { meter_id: 'purchase-fixture', revision: 1, source_fingerprint: 'a'.repeat(64),
    plan: { id: 'purchase-fixture', label: source === 'bwh' ? 'BWG 测试资源' : 'HOME 测试资源', source, scope: source === 'bwh' ? 'server' : 'external_node', quota_bytes: '400000000000', cycle: source === 'bwh' ? { kind: 'provider' } : { kind: 'fixed_days', days: 30, anchor: '2026-09-20T00:00:00+08:00' }, expires_on: '2027-01-19', accepted_initial_gap: true },
    consumption: { status: 'not_consumed', revision: null, consumed_at: null }, allowed_operations: source === 'bwh' ? ['expiry'] : ['quota', 'expiry', 'cycle'] }
  result.effective_plan = structuredClone(result.plan)
  return result
}
export function previewResult(body: any, base = plan()): any {
  const after = structuredClone(base.plan)
  if (body.operation === 'quota') after.quota_bytes = body.changes.add_bytes ? (BigInt(after.quota_bytes) + BigInt(body.changes.add_bytes)).toString() : body.changes.quota_bytes
  if (body.operation === 'expiry') after.expires_on = body.changes.expires_on
  if (body.operation === 'cycle') after.cycle = { kind: 'fixed_days', days: body.changes.days, anchor: body.changes.next_reset_at }
  return { ...base, operation: body.operation, changes: body.changes, before: structuredClone(base.plan), after, preview_token: 'signed-preview-fixture', effects: ['只更新此采购项目；已有用量与用户套餐保持不变。'] }
}
export const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); await vue.nextTick() }
export function mount(send: (path: string, method?: string, body?: any) => Promise<any>, enabled = true) {
  const props = vue.reactive({ bindingKey: 'purchase-fixture', enabled }), events: any[] = [], ends: Array<() => void> = []
  const scope = vue.effectScope(), exports: any = {}
  new Function('require', 'exports', compiled())((name: string) => name === 'vue' ? { ...vue, onUnmounted: (fn: () => void) => ends.push(fn) } : name === '../api' ? { ...api, request: send } : refreshModule(), exports)
  const state = scope.run(() => exports.default.setup(props, { expose: () => {}, emit: (name: string, value: any) => { if (name === 'saved') events.push(value) } }))
  return { state, props, events, unmount() { ends.forEach(fn => fn()); scope.stop() } }
}
export async function render(base = plan()) {
  const exports: any = {}
  new Function('require', 'exports', compiled(true))((name: string) => name === 'vue' ? vue : name === '../api' ? { ...api, request: async () => base } : refreshModule(), exports)
  const component = exports.default
  const wrapper = { ...component, async setup(props: any, context: any) { const result = component.setup(props, context); await flush(); return result } }
  const app = vue.createSSRApp(wrapper, { bindingKey: base.meter_id, enabled: true })
  app.component('ElButton', { setup(_: any, { attrs, slots }: any) { return () => vue.h('button', attrs, slots.default?.()) } })
  return renderToString(app)
}
