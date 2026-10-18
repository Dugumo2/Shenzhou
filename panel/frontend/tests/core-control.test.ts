import test from 'node:test'
import assert from 'node:assert/strict'
import { ApiError } from '../src/api.ts'
import { candidate, control, core, deferred, flush, job, mountCoreComponent } from './coreControlFixtures.ts'

test('核心详情保留原实例版本与节点关联，列表刷新不会跳走已选核心', async () => {
  const item = core()
  const ui = mountCoreComponent('ServerCoreDetails.vue', { cores: { items: [item], truncated: false } }, async () => ({}))
  ui.state.selectedId.value = item.id
  assert.equal(ui.state.selected.value.registered_version, '25.1.0')
  assert.equal(ui.state.selected.value.ingress_ids[0], 'ingress-one')
  ui.props.cores = { items: [{ ...item, name: '新显示别名', revision: 2 }], truncated: false }
  await flush()
  assert.equal(ui.state.selectedId.value, item.id)
  assert.equal(ui.state.selected.value.name, '新显示别名')
  ui.props.cores = { items: [], truncated: false }
  await flush()
  assert.equal(ui.state.selectedId.value, null)
  ui.unmount()
})

test('owner、执行绑定、写开关和服务器能力均不能被前端绕过', async () => {
  for (const overrides of [{ owner_state: 'observed_only' }, { binding: { available: false, reason: 'binding_unavailable' } },
    { write_enabled: false }, { busy: true }, { jobs: [job({ status: 'unknown' })] }]) {
    let writes = 0
    const ui = mountCoreComponent('CoreControlPanel.vue', { coreId: 'core-one' }, async (_path, method) => { if (method === 'POST') writes++; return control(overrides) })
    await flush(); await ui.state.submit('candidate'); await ui.state.submit('check')
    assert.equal(writes, 0); assert.equal(ui.state.ready.value, false); assert.ok(ui.state.limitation.value)
    ui.unmount()
  }
})

test('候选只发送有限日志字段，检查独立提交且队列回执不当应用成功', async () => {
  const calls: any[] = []
  let latest = control()
  const ui = mountCoreComponent('CoreControlPanel.vue', { coreId: 'core-one' }, async (path, method, body) => {
    if (method !== 'POST') return structuredClone(latest)
    calls.push({ path, body })
    if (path.endsWith('/candidates')) return { candidate: candidate(), replayed: false }
    latest = control({ jobs: [job()], busy: true }); return { job: job(), replayed: false }
  })
  await flush(); ui.state.level.value = 'warning'; await ui.state.submit('candidate')
  assert.deepEqual(Object.keys(calls[0].body).sort(), ['expected_revision', 'idempotency_key', 'patch'])
  assert.deepEqual(calls[0].body.patch, { log: { level: 'warning' } })
  assert.equal(calls.length, 1); assert.equal(ui.state.canApply.value, false)
  await ui.state.submit('check')
  assert.equal(calls[1].path, '/admin/cores/core-one/control/candidates/candidate-one/check')
  assert.equal(ui.state.detail.value.jobs[0].status, 'queued')
  assert.equal(ui.state.selected.value.state, 'prepared'); assert.equal(ui.state.canApply.value, false)
  assert.match(ui.state.notice.value, /任务已提交/); ui.unmount()
})

test('应用必须有有效检查和影响确认，UNKNOWN只读刷新后可识别真实恢复', async () => {
  const checked = candidate({ state: 'checked', checked_at: '2026-10-18T11:01:00Z', capabilities: { check: true, apply: true, rollback: false } })
  let latest = control({ candidates: [checked] }), writes = 0
  const ui = mountCoreComponent('CoreControlPanel.vue', { coreId: 'core-one' }, async (_path, method) => {
    if (method !== 'POST') return structuredClone(latest)
    writes++; latest = control({ candidates: [checked], jobs: [job({ action: 'apply', status: 'unknown' })], busy: true })
    return { job: job({ action: 'apply', status: 'unknown' }), replayed: false }
  })
  await flush(); await ui.state.submit('apply'); assert.equal(writes, 0)
  ui.state.impactConfirmed.value = true; await ui.state.submit('apply')
  assert.equal(writes, 1); assert.match(ui.state.limitation.value, /结果未知/)
  ui.state.impactConfirmed.value = true
  for (const action of ['candidate', 'check', 'apply', 'rollback']) await ui.state.submit(action)
  assert.equal(writes, 1)
  latest = control({ revision: 2, configuration_version: 'b'.repeat(64), current_applied_candidate_id: 'candidate-one',
    candidates: [candidate({ state: 'applied', capabilities: { check: false, apply: false, rollback: true } })],
    jobs: [job({ action: 'apply', status: 'succeeded', result: { config_sha256: 'b'.repeat(64) } })] })
  await ui.state.refresh()
  assert.equal(ui.state.canRollback.value, true); assert.equal(writes, 1)
  ui.unmount()
})

test('历史applied、无检查时间、旧修订和服务端拒绝均不显示可应用或回滚能力', async () => {
  for (const latest of [control({ revision: 2 }), control({ candidates: [candidate({ state: 'checked' })] }),
    control({ current_applied_candidate_id: 'another-candidate', candidates: [candidate({ state: 'applied', capabilities: { check: false, apply: false, rollback: true } })] })]) {
    const ui = mountCoreComponent('CoreControlPanel.vue', { coreId: 'core-one' }, async () => latest)
    await flush(); assert.equal(ui.state.canApply.value, false); assert.equal(ui.state.canRollback.value, false); ui.unmount()
  }
})

test('通信未知保留输入并锁住新写入，刷新不能把未确认原请求当未执行', async () => {
  let writes = 0
  const ui = mountCoreComponent('CoreControlPanel.vue', { coreId: 'core-one' }, async (_path, method) => {
    if (method === 'POST') { writes++; throw new ApiError(0, 'NETWORK_ERROR', '连接断开') }
    return control()
  })
  await flush(); ui.state.level.value = 'warning'; await ui.state.submit('candidate')
  assert.equal(ui.state.uncertain.value, true); assert.equal(ui.state.level.value, 'warning')
  await ui.state.refresh(); await ui.state.submit('candidate'); await ui.state.submit('check')
  assert.equal(writes, 1); assert.match(ui.state.writeError.value, /回执未知/); ui.unmount()
})

test('刷新失败保留旧候选与输入，403禁止写入，后续成功读取可恢复', async () => {
  let failure: unknown = null
  const ui = mountCoreComponent('CoreControlPanel.vue', { coreId: 'core-one' }, async () => { if (failure) throw failure; return control() })
  await flush(); ui.state.level.value = 'error'
  failure = new ApiError(403, 'permission_denied', '权限已撤销'); await ui.state.refresh()
  assert.equal(ui.state.selected.value.id, 'candidate-one'); assert.equal(ui.state.level.value, 'error')
  assert.equal(ui.state.permissionDenied.value, true); assert.equal(ui.state.ready.value, false)
  failure = null; await ui.state.refresh(); assert.equal(ui.state.ready.value, true); ui.unmount()
})

test('切换核心后迟到读取不能串入第二实例', async () => {
  const pending = deferred<any>()
  const ui = mountCoreComponent('CoreControlPanel.vue', { coreId: 'core-one' }, async path => path.includes('core-one') ? pending.promise : control({ core_id: 'core-two', candidates: [] }))
  ui.props.coreId = 'core-two'; await flush()
  pending.resolve(control()); await flush()
  assert.equal(ui.state.detail.value.core_id, 'core-two'); assert.equal(ui.state.selectedId.value, '')
  ui.unmount()
})

test('连点提交只能入队一次，迟到写回执不污染新目标', async () => {
  const pending = deferred<any>(); let writes = 0
  const ui = mountCoreComponent('CoreControlPanel.vue', { coreId: 'core-one' }, async (path, method) => {
    if (method === 'POST') { writes++; return pending.promise }
    return control({ core_id: path.includes('core-two') ? 'core-two' : 'core-one' })
  })
  await flush(); const saving = ui.state.submit('check'); await ui.state.submit('check'); assert.equal(writes, 1)
  ui.props.coreId = 'core-two'; await flush(); pending.resolve({ job: job(), replayed: false }); await saving
  assert.equal(ui.state.detail.value.core_id, 'core-two'); assert.equal(ui.state.notice.value, '')
  ui.unmount()
})

test('真实请求时限触发后进入UNKNOWN而不是失败重试', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  let writes = 0
  const ui = mountCoreComponent('CoreControlPanel.vue', { coreId: 'core-one' }, async (_path, method, _body, signal) => {
    if (method !== 'POST') return control()
    writes++
    return new Promise((_resolve, reject) => signal?.addEventListener('abort', () => reject(new ApiError(0, 'REQUEST_ABORTED', '超时')), { once: true }))
  })
  await flush(); const saving = ui.state.submit('check'); t.mock.timers.tick(15000); await saving
  assert.equal(ui.state.writing.value, false); assert.equal(ui.state.uncertain.value, true)
  await ui.state.submit('check'); assert.equal(writes, 1); assert.match(ui.state.writeError.value, /回执未知/)
  ui.unmount(); t.mock.timers.reset()
})
