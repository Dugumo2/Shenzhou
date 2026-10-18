import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'
import * as vue from 'vue'
import { usageResponseIsCurrent } from '../src/usageTemporal.ts'
import { createSnapshotRequest } from '../src/snapshotRequest.ts'
import type { UsageOverviewData } from '../src/usage-types.ts'

test('同会话重新读取不清范围缓存，退出/换身份/权限/令牌立即清理', () => {
  const source = readFileSync(new URL('../src/auth.ts', import.meta.url), 'utf8')
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
  const exports: any = {}; let clears = 0, navigationClears = 0
  new Function('require', 'exports', code)((name: string) => name === 'vue' ? vue
    : name === './usageRangeCache' ? { usageRangeCache: { clear: () => clears++ } }
    : name === './usageNavigation' ? { usageNavigation: { clear: () => navigationClears++ } }
    : { setCsrfToken() {} }, exports)
  const member = () => ({ authenticated: true, user: { username: 'fixture', is_staff: true }, csrf_token: 'fixture-only' })
  exports.applySession(member()); const epoch = exports.sessionScope.generation
  exports.applySession(member())
  assert.equal(exports.sessionScope.generation, epoch)
  assert.equal(clears, 1)
  exports.applySession({ ...member(), user: { username: 'fixture', is_staff: false } })
  exports.applySession({ ...member(), csrf_token: 'fixture-rotated' })
  exports.clearSession()
  assert.equal(clears, 4); assert.equal(navigationClears, 4)
})

test('成功缓存预置后重验保旧且去重，换身份不借用缓存', async () => {
  let finish!: (v: { bytes: string }) => void
  const reader = createSnapshotRequest<{ bytes: string }>({ read: () => new Promise(resolve => { finish = resolve }) })
  reader.prime('/usage?period=month', 'session:1', { bytes: '99' }, '2026-10-18T00:00:00Z')
  const first = reader.load('/usage?period=month', 'session:1')
  assert.equal(reader.state.data?.bytes, '99'); assert.equal(reader.state.busy, true)
  assert.equal(reader.load('/usage?period=month', 'session:1'), first)
  await Promise.resolve(); finish({ bytes: '100' }); await first
  const other = reader.load('/usage?period=month', 'session:2')
  assert.equal(reader.state.data, null)
  reader.dispose(); await other
})

test('日界和套餐重置之后10毫秒返回旧响应仍拒收，正常部分覆盖不拒收', () => {
  const value = { generated_at: '2026-10-19T23:59:59+08:00', summary: { next_reset_at: '2026-10-20T00:00:00+08:00' },
    current_cycle: { ends_at: '2026-10-20T00:00:00+08:00' }, quality: { state: 'gap' } } as UsageOverviewData
  assert.equal(usageResponseIsCurrent(value, Date.parse('2026-10-19T23:59:59.500+08:00')), true)
  assert.equal(usageResponseIsCurrent(value, Date.parse('2026-10-20T00:00:00.010+08:00')), false)
  assert.equal(usageResponseIsCurrent({ ...value, generated_at: '2026-10-20T00:00:00.001+08:00' }, Date.parse('2026-10-20T00:00:00.010+08:00')), false)
})
