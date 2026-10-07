import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { effectScope } from 'vue'
import { createSnapshotRequest } from '../src/snapshotRequest.ts'
import { useSnapshotRequest } from '../src/useSnapshotRequest.ts'
import { ApiError, setUnauthorizedHandler } from '../src/api.ts'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
type Sample = { value: number; sampled_at: string }
const sample = (value: number): Sample => ({ value, sampled_at: '2026-10-04T05:00:00Z' })
const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch; setUnauthorizedHandler(() => {}) })

test('首次慢请求用骨架，连续点击同路径只读取一次并共享 Promise', async () => {
  const response = deferred<Sample>()
  let calls = 0
  const reader = createSnapshotRequest({ read: () => { calls++; return response.promise } })
  const first = reader.load('/me/services/a', 'user-a')
  assert.equal(reader.state.busy, true)
  assert.equal(reader.state.hasLoaded, false)
  assert.equal(reader.state.data, null)
  assert.equal(reader.load('/me/services/a', 'user-a'), first)
  await Promise.resolve()
  assert.equal(calls, 1)
  response.resolve(sample(10))
  assert.deepEqual(await first, sample(10))
  assert.equal(reader.state.busy, false)
  assert.equal(reader.state.hasLoaded, true)
})

test('同 key 刷新及失败保留原数据和采样时间；恢复仅更新读取时间', async () => {
  const responses = [deferred<Sample>(), deferred<Sample>(), deferred<Sample>()]
  let index = 0
  let time = '2026-10-07T05:00:00Z'
  const reader = createSnapshotRequest({ read: () => responses[index++]!.promise, now: () => time })
  let operation = reader.load('/usage?period=current')
  await Promise.resolve(); responses[0]!.resolve(sample(10)); await operation
  const original = reader.state.data
  operation = reader.load('/usage?period=current')
  assert.equal(reader.state.data, original)
  assert.equal(reader.state.hasLoaded, true)
  await Promise.resolve(); responses[1]!.reject(new Error('暂时失败')); await operation
  assert.equal(reader.state.data, original)
  assert.equal(reader.state.lastReadAt, '2026-10-07T05:00:00Z')
  assert.equal(reader.state.data?.sampled_at, '2026-10-04T05:00:00Z')
  assert.ok(reader.state.error)
  time = '2026-10-07T06:00:00Z'
  operation = reader.load('/usage?period=current')
  assert.equal(reader.state.error, null)
  await Promise.resolve(); responses[2]!.resolve(sample(11)); await operation
  assert.equal(reader.state.data?.value, 11)
  assert.equal(reader.state.data?.sampled_at, '2026-10-04T05:00:00Z')
  assert.equal(reader.state.lastReadAt, time)
})

test('A→B→A 迟到结果既不覆盖当前值，也不解除当前加载状态', async () => {
  const responses = [deferred<Sample>(), deferred<Sample>(), deferred<Sample>()]
  let index = 0
  const reader = createSnapshotRequest({ read: () => responses[index++]!.promise })
  const firstA = reader.load('/services/a'); await Promise.resolve()
  const firstB = reader.load('/services/b'); await Promise.resolve()
  const currentA = reader.load('/services/a'); await Promise.resolve()
  responses[0]!.resolve(sample(1)); assert.equal(await firstA, undefined)
  responses[1]!.reject(new Error('B迟到失败')); assert.equal(await firstB, undefined)
  assert.equal(reader.state.data, null)
  assert.equal(reader.state.error, null)
  assert.equal(reader.state.busy, true)
  responses[2]!.resolve(sample(3)); await currentA
  assert.deepEqual(reader.state.data, sample(3))
})

test('筛选、分页或账号切换立即清除旧内容和读取时间，失败不借用旧对象', async () => {
  let fail = false
  const reader = createSnapshotRequest({ read: async () => { if (fail) throw new Error('无权限'); return sample(1) } })
  await reader.load('/usage?period=current&page=1', 'user-a')
  for (const [path, user] of [['/usage?period=7d&page=1', 'user-a'], ['/usage?period=7d&page=2', 'user-a'], ['/usage?period=7d&page=2', 'user-b']]) {
    fail = true
    const reading = reader.load(path!, user!)
    assert.equal(reader.state.data, null)
    assert.equal(reader.state.lastReadAt, null)
    await reading
    assert.equal(reader.state.data, null)
    fail = false
    await reader.load(path!, user!)
  }
})

test('reset 使待返回的旧身份数据失效；dispose 后不继续请求或通知', async () => {
  const response = deferred<Sample>()
  let calls = 0, notifications = 0
  const reader = createSnapshotRequest({ read: () => { calls++; return response.promise } })
  reader.subscribe(() => { notifications++ })
  const reading = reader.load('/services/a', 'user-a'); await Promise.resolve()
  reader.reset()
  response.resolve(sample(1)); assert.equal(await reading, undefined)
  assert.equal(reader.state.data, null)
  reader.dispose()
  const previousNotifications = notifications
  assert.equal(await reader.load('/services/a', 'user-b'), undefined)
  assert.equal(calls, 1)
  assert.equal(notifications, previousNotifications)
})

test('同步读取异常也进入失败状态，空集合成功仍按已加载处理', async () => {
  let fail = true
  const reader = createSnapshotRequest({ read: () => { if (fail) throw new Error('同步失败'); return Promise.resolve([]) } })
  await reader.load('/services')
  assert.equal(reader.state.busy, false)
  assert.ok(reader.state.error)
  fail = false
  await reader.load('/services')
  assert.equal(reader.state.hasLoaded, true)
  assert.deepEqual(reader.state.data, [])
})

test('默认读取固定 GET，401 仍触发现有会话失效主流程', async () => {
  let expired = 0, calls = 0
  setUnauthorizedHandler(() => { expired++ })
  globalThis.fetch = async (url, options) => {
    calls++
    assert.equal(url, '/api/v1/me/services')
    assert.equal(options?.method, 'GET')
    assert.equal(options?.body, undefined)
    return Response.json({ error: { code: 'AUTH_REQUIRED', message: '请重新登录。' } }, { status: 401 })
  }
  const reader = createSnapshotRequest()
  await reader.load('/me/services')
  assert.equal(expired, 1)
  assert.equal(calls, 1)
  assert.ok(reader.state.error instanceof ApiError)
})

test('Vue 薄适配区分首次/刷新，作用域卸载隔离迟到响应', async () => {
  const response = deferred<Sample>()
  let readIndex = 0
  const scope = effectScope()
  const state = scope.run(() => useSnapshotRequest({ read: () => ++readIndex === 1 ? Promise.resolve(sample(1)) : response.promise }))!
  const first = state.load('/usage')
  assert.equal(state.initialLoading.value, true)
  assert.equal(state.refreshing.value, false)
  await first
  const next = state.load('/usage'); await Promise.resolve()
  assert.equal(state.initialLoading.value, false)
  assert.equal(state.refreshing.value, true)
  assert.equal(state.data.value?.value, 1)
  scope.stop()
  response.resolve(sample(2)); await next
  assert.equal(state.data.value, null)
  assert.equal(state.lastReadAt.value, null)
  assert.equal(state.busy.value, false)
})

test('401/403/404 撤除旧服务与读取时间，网络及5xx保留可见快照', async () => {
  for (const status of [0, 401, 403, 404, 500, 503]) {
    let fail = false
    const issue = new ApiError(status, 'READ_FAILED', '读取失败')
    const reader = createSnapshotRequest({ read: async () => { if (fail) throw issue; return sample(1) } })
    await reader.load('/me/services/a', 'same-account')
    const originalTime = reader.state.lastReadAt
    fail = true
    await reader.load('/me/services/a', 'same-account')
    assert.equal(reader.state.error, issue)
    assert.equal(reader.state.busy, false)
    if ([401, 403, 404].includes(status)) {
      assert.equal(reader.state.data, null)
      assert.equal(reader.state.lastReadAt, null)
      assert.equal(reader.state.hasLoaded, false)
    } else {
      assert.deepEqual(reader.state.data, sample(1))
      assert.equal(reader.state.lastReadAt, originalTime)
      assert.equal(reader.state.hasLoaded, true)
    }
  }
})

test('同账号会话或角色变化后，旧身份成功和失败响应均不能覆盖新值', async () => {
  const responses = [deferred<Sample>(), deferred<Sample>(), deferred<Sample>()]
  let index = 0
  const reader = createSnapshotRequest({ read: () => responses[index++]!.promise })
  const first = reader.load('/me/services/a', 'user-a:session-1:admin'); await Promise.resolve()
  const second = reader.load('/me/services/a', 'user-a:session-2:admin'); await Promise.resolve()
  const current = reader.load('/me/services/a', 'user-a:session-2:user'); await Promise.resolve()
  responses[2]!.resolve(sample(3)); await current
  responses[0]!.resolve(sample(1)); await first
  responses[1]!.reject(new ApiError(403, 'FORBIDDEN', '权限已变化')); await second
  assert.deepEqual(reader.state.data, sample(3))
  assert.equal(reader.state.error, null)
})

test('默认15秒结束永不返回的GET，释放busy与同key重试入口', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  let signal: AbortSignal | undefined
  const reader = createSnapshotRequest({ read: (_path, currentSignal) => { signal = currentSignal; return new Promise<Sample>(() => {}) } })
  const waiting = reader.load('/usage'); await Promise.resolve()
  context.mock.timers.tick(14_999)
  assert.equal(reader.state.busy, true)
  context.mock.timers.tick(1)
  assert.equal(await waiting, undefined)
  assert.equal(signal?.aborted, true)
  assert.equal(reader.state.busy, false)
  assert.equal(reader.state.hasLoaded, false)
  assert.ok(reader.state.error instanceof ApiError && reader.state.error.code === 'REQUEST_TIMEOUT')
  const retry = reader.load('/usage')
  assert.notEqual(retry, waiting)
  assert.equal(reader.state.busy, true)
  reader.dispose(); await retry
})

test('刷新超时保留原采样与读取时刻，重试成功后旧迟到响应不能覆盖', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  const late = deferred<Sample>()
  let count = 0
  const reader = createSnapshotRequest({ timeoutMs: 50, read: () => ++count === 2 ? late.promise : Promise.resolve(sample(count)) })
  await reader.load('/usage')
  const readAt = reader.state.lastReadAt
  const timeout = reader.load('/usage'); await Promise.resolve()
  context.mock.timers.tick(50); await timeout
  assert.deepEqual(reader.state.data, sample(1))
  assert.equal(reader.state.lastReadAt, readAt)
  assert.equal(reader.state.busy, false)
  await reader.load('/usage')
  late.resolve(sample(2)); await Promise.resolve(); await Promise.resolve()
  assert.deepEqual(reader.state.data, sample(3))
  assert.equal(reader.state.error, null)
})

test('换key/reset/dispose取消各自transport并结束忽略signal的旧等待，不取消别的实例', async () => {
  const signals: AbortSignal[] = []
  const read = (_path: string, signal?: AbortSignal) => { signals.push(signal!); return new Promise<Sample>(() => {}) }
  const first = createSnapshotRequest({ read }), independent = createSnapshotRequest({ read })
  const firstRead = first.load('/a'); await Promise.resolve()
  const independentRead = independent.load('/other'); await Promise.resolve()
  const next = first.load('/b'); await Promise.resolve()
  assert.equal(signals[0]!.aborted, true)
  assert.equal(signals[1]!.aborted, false)
  assert.equal(await firstRead, undefined)
  first.reset(); assert.equal(signals[2]!.aborted, true); await next
  independent.dispose(); assert.equal(signals[1]!.aborted, true); await independentRead
})

test('实际fetch返回无效403/404 envelope仍清除旧授权对象，不变成普通保旧失败', async () => {
  for (const status of [403, 404]) for (const body of [null, [], 1, 'invalid']) {
    globalThis.fetch = async () => Response.json({ data: sample(1) })
    const reader = createSnapshotRequest<Sample>()
    await reader.load('/me/services/a')
    globalThis.fetch = async () => Response.json(body, { status })
    await reader.load('/me/services/a')
    assert.equal(reader.state.data, null)
    assert.equal(reader.state.lastReadAt, null)
    assert.ok(reader.state.error instanceof ApiError && reader.state.error.status === status)
  }
})

test('实际fetch忽略取消后迟到401不退出新session，超时后旧401同样隔离', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  for (const reason of ['switch', 'timeout']) {
    const old = deferred<Response>()
    let calls = 0, expired = 0
    setUnauthorizedHandler(() => { expired++ })
    globalThis.fetch = () => ++calls === 1 ? old.promise : Promise.resolve(Response.json({ data: sample(2) }))
    const reader = createSnapshotRequest<Sample>({ timeoutMs: 50 })
    const oldRead = reader.load('/usage', 'session:1'); await Promise.resolve()
    if (reason === 'timeout') { context.mock.timers.tick(50); await oldRead }
    await reader.load('/usage', 'session:2')
    old.resolve(Response.json(null, { status: 401 }))
    await oldRead; await Promise.resolve(); await Promise.resolve()
    assert.equal(expired, 0)
    assert.deepEqual(reader.state.data, sample(2))
  }
})

test('成功读取在发布前调用reconcile，首次传null，同key传旧值并返回合并结果', async () => {
  const seen: Array<Sample | null> = []
  let count = 0
  const reader = createSnapshotRequest({
    read: async () => sample(++count),
    reconcile: (next, previous) => { seen.push(previous); return { ...next, value: next.value + (previous?.value ?? 0) } },
  })
  assert.deepEqual(await reader.load('/usage', 'session:1'), sample(1))
  assert.deepEqual(await reader.load('/usage', 'session:1'), sample(3))
  assert.deepEqual(seen, [null, sample(1)])
  assert.deepEqual(reader.state.data, sample(3))
})

test('换路径或identity时reconcile只收到null，不跨对象合并旧值', async () => {
  const seen: Array<Sample | null> = []
  const reader = createSnapshotRequest({ read: async () => sample(1), reconcile: (next, previous) => { seen.push(previous); return next } })
  await reader.load('/usage?period=current', 'session:1')
  await reader.load('/usage?period=7d', 'session:1')
  await reader.load('/usage?period=7d', 'session:2')
  assert.deepEqual(seen, [null, null, null])
})

test('generation失效或卸载后迟到结果不进入reconcile', async () => {
  const late = deferred<Sample>()
  let count = 0
  const seen: number[] = []
  const reader = createSnapshotRequest({ read: () => ++count === 1 ? late.promise : Promise.resolve(sample(2)), reconcile: next => { seen.push(next.value); return next } })
  const first = reader.load('/a'); await Promise.resolve()
  await reader.load('/b')
  late.resolve(sample(1)); await first; await Promise.resolve(); await Promise.resolve()
  assert.deepEqual(seen, [2])
  const afterDispose = deferred<Sample>()
  const disposed = createSnapshotRequest({ read: () => afterDispose.promise, reconcile: next => { seen.push(next.value); return next } })
  const pending = disposed.load('/c'); await Promise.resolve(); disposed.dispose()
  afterDispose.resolve(sample(3)); await pending; await Promise.resolve(); await Promise.resolve()
  assert.deepEqual(seen, [2])
})

test('reconcile抛错保留旧内容与时间、释放busy/pending，修复后同key可以重试', async () => {
  let fail = false, calls = 0
  const issue = new Error('合并失败')
  const reader = createSnapshotRequest({ read: async () => sample(++calls), reconcile: next => { if (fail) throw issue; return next } })
  await reader.load('/usage')
  const lastReadAt = reader.state.lastReadAt
  fail = true
  assert.equal(await reader.load('/usage'), undefined)
  assert.equal(reader.state.busy, false)
  assert.equal(reader.state.error, issue)
  assert.deepEqual(reader.state.data, sample(1))
  assert.equal(reader.state.lastReadAt, lastReadAt)
  fail = false
  await reader.load('/usage')
  assert.deepEqual(reader.state.data, sample(3))
  assert.equal(reader.state.error, null)
})
