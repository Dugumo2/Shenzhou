import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'

const require = createRequire(import.meta.url)
export const frontendRoot = fileURLToPath(new URL('../', import.meta.url))
export const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); await vue.nextTick() }
export function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done }); return { promise, resolve } }
export function core() {
  return { id: 'core-one', name: '现有 Xray', instance_key: 'xray-main', core_type: 'xray', registered_version: '25.1.0',
    configuration_owner: '已登记配置所有者', configuration_version: 'original-version', revision: 1, notes: '保留原备注',
    ingress_ids: ['ingress-one'], association_valid: true, service_unit: 'xray.service', owner_state: 'verified',
    evidence_source: '隔离夹具', evidence_observed_at: '2026-10-18T11:00:00Z' }
}
export function candidate(extra: Record<string, any> = {}) {
  return { id: 'candidate-one', core_id: 'core-one', base_revision: 1, state: 'prepared', patch: { log: { level: 'warning' } },
    difference: { field: 'log.level', before: 'info', after: 'warning' }, base_sha256: 'a'.repeat(64), candidate_sha256: 'b'.repeat(64),
    created_at: '2026-10-18T11:00:00Z', checked_at: null, capabilities: { check: true, apply: false, rollback: false },
    impact: { scope: 'single_core', may_restart: true, message: '仅更改此核心日志级别；应用或回滚可能重启该实例并中断其现有连接。' }, ...extra }
}
export function job(extra: Record<string, any> = {}) {
  return { id: 'job-one', core_id: 'core-one', candidate_id: 'candidate-one', action: 'check', status: 'queued',
    expected_revision: 1, fence: 1, result: {}, error_code: '', created_at: '2026-10-18T11:00:00Z', started_at: null, finished_at: null, ...extra }
}
export function control(extra: Record<string, any> = {}) {
  return { core_id: 'core-one', revision: 1, configuration_version: 'original-version', owner_state: 'verified',
    binding: { available: true, reason: '' }, busy: false, write_enabled: true, current_applied_candidate_id: null,
    capabilities: { create_candidate: true, supported_fields: ['log.level'], log_levels: ['debug', 'info', 'warning', 'error', 'none'] },
    candidates: [candidate()], jobs: [], limitations: [], ...extra }
}

// 编译真实 SFC 的脚本，直接驱动其事件；仅隔离接口及组件生命周期。
export function mountCoreComponent(entry: string, initialProps: Record<string, any>, send: (path: string, method?: string, body?: any, signal?: AbortSignal) => Promise<any>) {
  const ends: Array<() => void> = [], events: Array<[string, any]> = [], scope = vue.effectScope()
  const props = vue.reactive(initialProps)
  const modules = new Map<string, any>()
  function load(filename: string): any {
    if (modules.has(filename)) return modules.get(filename)
    const source = readFileSync(filename, 'utf8')
    const content = filename.endsWith('.vue') ? compileScript(parse(source).descriptor, { id: filename }).content : source
    const code = ts.transpileModule(content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
    const exports: Record<string, any> = {}; modules.set(filename, exports)
    new Function('require', 'exports', code)((name: string) => {
      if (name === 'vue') return { ...vue, onUnmounted: (fn: () => void) => ends.push(fn), onMounted: (fn: () => void) => fn() }
      if (/\/api(?:\.ts)?$/.test(name)) return { ...api, request: send }
      if (!name.startsWith('.')) return require(name)
      return load(resolve(dirname(filename), /\.(vue|ts)$/.test(name) ? name : name + '.ts'))
    }, exports)
    return exports
  }
  const component = load(resolve(frontendRoot, 'src/components', entry)).default
  const state = scope.run(() => component.setup(props, { expose: () => {}, emit: (name: string, value: any) => events.push([name, value]) }))
  return { state, props, events, unmount() { ends.forEach(fn => fn()); scope.stop() } }
}
