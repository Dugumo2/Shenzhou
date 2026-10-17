import test from 'node:test'
import assert from 'node:assert/strict'
import { createReleaseRecovery, safeRecoveryTarget, installReleaseRecovery, releaseRecovery, updateFrontendRelease } from '../src/releaseRecovery.ts'

const oldVersion = 'a'.repeat(64), newVersion = 'b'.repeat(64)
function fixture(overrides: Record<string, unknown> = {}) {
  const reloads: string[] = [], navigations: string[] = [], confirms: string[] = []
  const recovery = createReleaseRecovery({ version: oldVersion, fetchVersion: async () => newVersion,
    navigate: async target => { navigations.push(target) }, reload: target => { reloads.push(target) },
    confirm: message => { confirms.push(message); return false }, ...overrides })
  return { recovery, reloads, navigations, confirms }
}

test('持续模块404只提示一次用户主动更新，保留目标且不重发登录', async () => {
  const { recovery, reloads, navigations } = fixture()
  for (let i = 0; i < 3; i++) await recovery.check('/admin/overview?tab=usage')
  assert.equal(recovery.state.status, 'update'); assert.deepEqual(reloads, []); assert.deepEqual(navigations, [])
  recovery.update(); recovery.update()
  assert.deepEqual(reloads, ['/admin/overview?tab=usage'])
})

test('同版本失败不自动重载，网络恢复后仅用户重试导航', async () => {
  const { recovery, reloads, navigations } = fixture({ fetchVersion: async () => oldVersion })
  await recovery.check('/services/demo'); assert.equal(recovery.state.status, 'retry')
  recovery.update(); assert.deepEqual(reloads, [])
  await recovery.retry(); assert.deepEqual(navigations, ['/services/demo']); assert.equal(recovery.state.status, 'idle')
})

test('同版本与未知版本失败只在明确重新载入操作后刷新，连点仍只有一次', async () => {
  for (const overrides of [{ fetchVersion: async () => oldVersion }, { version: '' },
    { fetchVersion: async () => { throw new Error('offline') } }]) {
    const { recovery, reloads, navigations } = fixture(overrides)
    await recovery.check('/account'); assert.equal(recovery.state.status, 'retry')
    assert.deepEqual(reloads, []); assert.deepEqual(navigations, [])
    recovery.reload(); recovery.reload()
    assert.deepEqual(reloads, ['/account']); assert.deepEqual(navigations, [])
    assert.equal(recovery.state.updating, true)
  }
})

test('同版本重新载入取消确认保持脏输入和目标，确认后一次加载安全目标', async () => {
  let accepted = false
  const { recovery, reloads } = fixture({ fetchVersion: async () => oldVersion, confirm: () => accepted })
  recovery.markDirty(); await recovery.check('/admin/rules?mode=edit')
  recovery.reload()
  assert.deepEqual(reloads, []); assert.equal(recovery.state.dirty, true)
  assert.equal(recovery.state.updating, false); assert.equal(recovery.state.target, '/admin/rules?mode=edit')
  accepted = true; recovery.reload(); recovery.reload()
  assert.deepEqual(reloads, ['/admin/rules?mode=edit'])
})

test('重新载入入口只处理retry态，并再次拒绝不安全目标', async () => {
  let finish!: (version: string) => void
  const { recovery, reloads } = fixture({ fetchVersion: () => new Promise<string>(resolve => { finish = resolve }) })
  recovery.reload(); assert.deepEqual(reloads, [])
  const pending = recovery.check('/services'); recovery.reload(); assert.deepEqual(reloads, [])
  finish(newVersion); await pending; recovery.reload(); assert.deepEqual(reloads, [])
  const same = fixture({ fetchVersion: async () => oldVersion })
  await same.recovery.check('/services'); same.recovery.state.target = '//outside.example'
  same.recovery.reload(); assert.deepEqual(same.reloads, ['/services'])
})

test('未保存输入取消更新时保留原页面，不序列化输入，确认后仅重载一次', async () => {
  let accepted = false
  const { recovery, reloads } = fixture({ confirm: () => accepted })
  recovery.markDirty(); await recovery.check('/admin/rules')
  recovery.update(); assert.deepEqual(reloads, []); assert.equal(recovery.state.dirty, true)
  accepted = true; recovery.update(); assert.deepEqual(reloads, ['/admin/rules'])
})

test('未保存输入取消重试时不导航离开当前表单', async () => {
  const { recovery, navigations } = fixture({ fetchVersion: async () => oldVersion })
  recovery.markDirty(); await recovery.check('/admin/rules'); await recovery.retry()
  assert.deepEqual(navigations, []); assert.equal(recovery.state.dirty, true)
})

test('版本读取悬挂有截止时间，迟到响应不恢复更新按钮', async () => {
  let finish!: (version: string) => void, signal!: AbortSignal
  const { recovery, reloads } = fixture({ timeoutMs: 5, fetchVersion: (value: AbortSignal) => {
    signal = value; return new Promise<string>(resolve => { finish = resolve })
  } })
  await recovery.check('/services'); assert.equal(signal.aborted, true); assert.equal(recovery.state.status, 'retry')
  finish(newVersion); await Promise.resolve(); recovery.update(); assert.deepEqual(reloads, [])
  assert.equal(recovery.state.status, 'retry')
})

test('重复预加载和路由错误合并GET并保留最终目标', async () => {
  let finish!: (version: string) => void, reads = 0
  const { recovery, reloads } = fixture({ fetchVersion: () => { reads++; return new Promise<string>(resolve => { finish = resolve }) } })
  const first = recovery.check('/login'), second = recovery.check('/services/demo')
  finish(newVersion); await Promise.all([first, second]); assert.equal(reads, 1)
  recovery.update(); assert.deepEqual(reloads, ['/services/demo'])
})

test('新路由已成功时迟到版本响应不打断页面', async () => {
  let finish!: (version: string) => void
  const { recovery } = fixture({ fetchVersion: () => new Promise<string>(resolve => { finish = resolve }) })
  const request = recovery.check('/services/demo'); recovery.recovered(); finish(newVersion); await request
  assert.equal(recovery.state.status, 'idle')
})

test('版本读取断网、未知版本和无效响应不触发自动刷新', async () => {
  for (const overrides of [{ fetchVersion: async () => { throw new Error('offline') } },
    { version: '' }, { fetchVersion: async () => '<html>not version</html>' }]) {
    const { recovery, reloads } = fixture(overrides)
    await recovery.check('/services'); recovery.update()
    assert.equal(recovery.state.status, 'retry'); assert.deepEqual(reloads, [])
  }
})

test('持续路由失败重试有明确反馈且不循环调用导航', async () => {
  let calls = 0
  const { recovery, reloads } = fixture({ fetchVersion: async () => oldVersion,
    navigate: async () => { calls++; throw new Error('Failed to fetch dynamically imported module') } })
  await recovery.check('/services/demo'); await recovery.retry()
  assert.equal(calls, 1); assert.equal(recovery.state.status, 'retry'); assert.deepEqual(reloads, [])
})

test('安全目标拒绝外站、协议相对、编码反斜杠和控制字符', () => {
  for (const target of ['https://evil.example/', '//evil.example', '/%2fevil.example', '/%5cevil', '/a\n', '/%00', '/%zz', null]) {
    assert.equal(safeRecoveryTarget(target), '/services')
  }
  assert.equal(safeRecoveryTarget('/services/demo?tab=usage'), '/services/demo?tab=usage')
})

test('实际安装接线：预加载与路由失败只GET一次，取消脏输入不刷新，确认后取新HTML', async () => {
  const names = ['document', 'window', 'fetch', 'Element'] as const
  const saved = names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)] as const)
  const events = new Map<string, (event: unknown) => void>()
  let onError!: (_error: unknown, to: { fullPath: string }) => void
  let afterEach!: (_to: unknown, _from: unknown, failure: unknown) => void
  let finish!: (value: unknown) => void, consent = false, reloads = 0
  const requests: Array<{ url: unknown; options: RequestInit }> = [], targets: string[] = []
  class Field { closest() { return this } }
  const globals = {
    document: { querySelector: () => ({ content: oldVersion }), addEventListener: (name: string, handler: (event: unknown) => void) => events.set(name, handler) },
    Element: Field,
    window: { addEventListener: (name: string, handler: (event: unknown) => void) => events.set(name, handler),
      confirm: () => consent, history: { replaceState: (_state: unknown, _unused: unknown, target: string) => targets.push(target) },
      location: { reload: () => { reloads++ } } },
    fetch: (url: unknown, options: RequestInit) => { requests.push({ url, options }); return new Promise(resolve => { finish = resolve }) },
  }
  try {
    for (const name of names) Object.defineProperty(globalThis, name, { configurable: true, writable: true, value: globals[name] })
    Object.assign(releaseRecovery, { status: 'idle', message: '', dirty: false, updating: false })
    installReleaseRecovery({ currentRoute: { value: { fullPath: '/login' } }, onError: (fn: typeof onError) => { onError = fn },
      afterEach: (fn: typeof afterEach) => { afterEach = fn }, replace: async () => {} } as unknown as Parameters<typeof installReleaseRecovery>[0])
    events.get('input')!({ target: new Field() })
    events.get('vite:preloadError')!({}); onError(new Error('module404'), { fullPath: '/services/demo' })
    finish({ ok: true, json: async () => ({ schema_version: 1, version: newVersion }) })
    for (let i = 0; i < 12; i++) await Promise.resolve()
    assert.equal(requests.length, 1); assert.equal(requests[0]?.url, '/app/release-version.json')
    assert.equal(requests[0]?.options.method, 'GET'); assert.equal(requests[0]?.options.cache, 'no-store')
    updateFrontendRelease(); assert.equal(reloads, 0)
    consent = true; updateFrontendRelease(); updateFrontendRelease()
    assert.deepEqual(targets, ['/app/#/services/demo']); assert.equal(reloads, 1)
    afterEach({}, {}, undefined); assert.equal(releaseRecovery.status, 'idle')
  } finally {
    for (const [name, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor)
      else Reflect.deleteProperty(globalThis, name)
    }
  }
})
