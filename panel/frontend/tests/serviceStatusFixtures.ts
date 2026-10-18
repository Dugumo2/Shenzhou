import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse, compileScript, compileStyle } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'

const require = createRequire(import.meta.url)
export const sourceRoot = fileURLToPath(new URL('../src/', import.meta.url))

export function service(extra: Record<string, unknown> = {}) {
  return { id: 'a1b3da45-2222-4333-8444-555555555555', name: '原有套餐', source_type: 'p8',
    quota_bytes: '1000000000000', used_bytes: null, remaining_bytes: null, raw_bytes: null,
    usage_estimate: { state: 'estimated', used_bytes: '98253546067', remaining_bytes: '901746453933', upload_bytes: '33734964678', download_bytes: '64518581389' },
    next_reset_at: '2026-10-20T00:00:00+08:00', expires_at: '2027-12-30T10:49:00+08:00',
    state: 'active', enabled: true, business_state: 'metering_gap', status_label: '用量待核算', quota_state: 'applied',
    service_status: { state: 'active', label: '已应用', message: '登记已应用，不代表线路连通。' },
    usage: { quality: 'gap', updated_at: '2026-10-18T07:00:00Z', message: '内部采样覆盖未确认与迁入来源说明' },
    delivery: { state: 'ready', message: '', download_url: null }, ...extra }
}

export function diagnostics() {
  return { service_id: service().id, period: '7d', generated_at: '2026-10-18T08:00:00Z', service_status: service().service_status,
    metering: { quality: 'gap', evidence_quality: 'partial_observed', updated_at: '2026-10-18T07:00:00Z', persisted_gap: false,
      active_identity_count: 8, evidence_identity_count: 8, unverified_identity_count: 0, ambiguous_identity_count: 0, partial_observed_identity_count: 8,
      reason_codes: ['partial_observed_evidence', 'missing_complete_evidence'] },
    historical_baseline: { state: 'applied', charged_bytes: '98253546067', source_observed_at: '2026-10-13T01:00:00Z' },
    coverage: { state: 'partial', message_code: 'partial_observed', range_start: '2026-10-12T00:00:00+08:00', range_end: '2026-10-18T15:00:00+08:00', unallocated_charged_bytes: '4000000000', boundary_pending_count: 2 },
    limitations: { lossless_tail_verified: false, hard_limit_verified: false, revocation_verified: false } }
}

// 渲染真实组件；仅接口数据使用隔离夹具，不替换模板或数据分支。
export async function render(entry: string, props: Record<string, unknown> = {}, item = service()) {
  const modules = new Map<string, any>(), styles: string[] = []
  const auth = vue.reactive({ session: { authenticated: true, user: { username: 'status-fixture', is_staff: false }, csrf_token: 'fixture' } })
  function load(filename: string): any {
    if (modules.has(filename)) return modules.get(filename)
    let source = readFileSync(filename, 'utf8')
    if (filename.endsWith('.vue')) {
      const { descriptor } = parse(source)
      // 使用组件原有样式检验窄屏布局；仅去掉作用域属性以适配SSR夹具。
      for (const style of descriptor.styles) styles.push(compileStyle({ source: style.content, filename, id: 'status-fixture', scoped: false }).code)
      source = compileScript(descriptor, { id: filename, inlineTemplate: true }).content
    }
    const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
    const exports: Record<string, any> = {}; modules.set(filename, exports)
    new Function('require', 'exports', code)((name: string) => {
      if (name === 'vue') return vue
      if (/\/api(?:\.ts)?$/.test(name)) return { ...api, request: async (path: string) => {
        assert.equal(path, '/me/services')
        return { items: [item], compatibility: { state: 'ready', message: null } }
      } }
      if (/\/auth(?:\.ts)?$/.test(name)) return { auth }
      if (!name.startsWith('.')) return require(name)
      return load(resolve(dirname(filename), /\.(vue|ts)$/.test(name) ? name : name + '.ts'))
    }, exports)
    return exports
  }
  const component = load(resolve(sourceRoot, entry)).default
  const wrapper = { ...component, async setup(input: any, context: any) {
    const result = component.setup(input, context)
    for (let i = 0; i < 40; i++) await Promise.resolve()
    return result
  } }
  const app = vue.createSSRApp(wrapper, props)
  for (const name of ['el-alert', 'el-skeleton', 'el-tag', 'el-progress']) app.component(name, {
    props: ['title'], setup(p: any, { slots }: any) { return () => vue.h('div', [p.title, slots.default?.()]) },
  })
  app.component('el-button', { setup(_p: any, { slots }: any) { return () => vue.h('button', slots.default?.()) } })
  app.component('RouterLink', { props: ['to'], setup(p: any, { slots }: any) { return () => vue.h('a', { href: p.to }, slots.default?.()) } })
  return { html: await renderToString(app), css: styles.join('\n') }
}
