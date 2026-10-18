import test from 'node:test'
import assert from 'node:assert/strict'
import { ApiError } from '../src/api.ts'
import { mount, plan, previewResult, flush, render } from './resourcePlanFixtures.ts'

test('额度、续费和周期各自预览并显式确认，追加精确到字节', async () => {
  const writes: any[] = [], initial = plan()
  const ui = mount(async (path, method, body) => {
    if (method !== 'POST') return initial
    writes.push({ path, body: JSON.parse(JSON.stringify(body)) })
    if (path.endsWith('/preview')) return previewResult(body)
    return { ...initial, revision: 2, consumption: { status: 'pending', revision: null, consumed_at: null }, receipt: { id: 'receipt', revision: 2, status: 'pending', replayed: false } }
  })
  await flush()
  ui.state.quotaMode.value = 'add'; ui.state.additionGB.value = '9007199.254740993'
  await ui.state.makePreview(); assert.equal(writes.length, 1)
  assert.deepEqual(writes[0].body.changes, { add_bytes: '9007199254740993' })
  await ui.state.confirm(); assert.equal(writes.length, 2); assert.ok(writes[1].body.idempotency_key)
  assert.equal(ui.state.current.value.consumption.status, 'pending'); assert.equal(ui.events.length, 1)
  ui.state.changeOperation('expiry'); ui.state.expiresOn.value = '2028-04-01'; await ui.state.makePreview()
  assert.deepEqual(writes[2].body.changes, { expires_on: '2028-04-01' })
  ui.state.changeOperation('cycle'); ui.state.cycleDays.value = '31'; ui.state.nextReset.value = '2026-11-19T00:00'; await ui.state.makePreview()
  assert.deepEqual(writes[3].body.changes, { days: 31, next_reset_at: '2026-11-19T00:00:00+08:00' })
  assert.equal(initial.plan.quota_bytes, '400000000000'); assert.equal(initial.plan.expires_on, '2027-01-19')
  ui.unmount()
})

test('修改输入会撤掉旧预览，不能确认未预览的新值', async () => {
  let confirms = 0
  const ui = mount(async (path, method, body) => { if (method !== 'POST') return plan(); if (path.endsWith('/preview')) return previewResult(body); confirms++; return {} })
  await flush(); await ui.state.makePreview(); assert.ok(ui.state.preview.value)
  ui.state.quotaGB.value = '500'; await ui.state.confirm(); assert.equal(confirms, 0); assert.equal(ui.state.preview.value, null)
  ui.unmount()
})

test('成功HTTP却没有有效保存回执不能显示保存成功，保留原请求', async () => {
  const ui = mount(async (path, method, body) => { if (method !== 'POST') return plan(); return path.endsWith('/preview') ? previewResult(body) : { ...plan(), revision: 2 } })
  await flush(); await ui.state.makePreview(); await ui.state.confirm()
  assert.equal(ui.events.length, 0); assert.equal(ui.state.receipt.value, null); assert.ok(ui.state.pending.value)
  assert.match(ui.state.error.value, /缺少有效回执/); ui.unmount()
})

test('网络不确定时保留相同确认payload和key，只能显式重试原请求', async () => {
  const confirms: any[] = []
  const ui = mount(async (path, method, body) => {
    if (method !== 'POST') return plan()
    if (path.endsWith('/preview')) return previewResult(body)
    confirms.push(JSON.parse(JSON.stringify(body)))
    if (confirms.length === 1) throw new ApiError(0, 'NETWORK_ERROR', '连接中断')
    return { ...plan(), revision: 2, consumption: { status: 'pending', revision: null, consumed_at: null }, receipt: { id: 'one', revision: 2, status: 'pending', replayed: true } }
  })
  await flush(); ui.state.quotaGB.value = '500'; await ui.state.makePreview(); await ui.state.confirm()
  assert.equal(ui.state.quotaGB.value, '500'); assert.equal(ui.state.editable.value, false); assert.equal(ui.events.length, 0)
  await ui.state.makePreview(); await ui.state.confirm(); assert.deepEqual(confirms[0], confirms[1]); assert.equal(ui.state.receipt.value.replayed, true)
  ui.unmount()
})

test('409保输入及原key，必须读当前版本并显式采用后重新预览', async () => {
  const confirms: any[] = [], previews: any[] = []; let reads = 0
  const ui = mount(async (path, method, body) => {
    if (method !== 'POST') return { ...plan(), revision: ++reads }
    if (path.endsWith('/preview')) { previews.push(body); return previewResult(body, { ...plan(), revision: body.expected_revision }) }
    confirms.push(JSON.parse(JSON.stringify(body))); throw new ApiError(409, 'revision_conflict', '采购计划已变化')
  })
  await flush(); ui.state.quotaGB.value = '600'; await ui.state.makePreview(); await ui.state.confirm()
  assert.equal(ui.state.conflict.value, true); const key = ui.state.pending.value.idempotency_key
  await ui.state.confirm(); assert.equal(confirms.length, 1); assert.equal(ui.state.pending.value.idempotency_key, key)
  await ui.state.load(true); assert.equal(ui.state.quotaGB.value, '600'); assert.equal(ui.state.latest.value.revision, 2)
  ui.state.adoptLatest(); assert.equal(confirms.length, 1); assert.equal(ui.state.quotaGB.value, '600'); assert.equal(ui.state.preview.value, null)
  await ui.state.makePreview(); assert.equal(previews[1].expected_revision, 2)
  ui.unmount()
})

test('官方额度和reset只读，仅到期登记可以预览', async () => {
  const requests: any[] = []
  const ui = mount(async (_path, method, body) => { if (method !== 'POST') return plan('bwh'); requests.push(body); return previewResult(body, plan('bwh')) })
  await flush(); assert.equal(ui.state.operation.value, 'expiry')
  for (const operation of ['quota', 'cycle']) { ui.state.changeOperation(operation); await ui.state.makePreview() }
  assert.equal(requests.length, 0)
  ui.state.changeOperation('expiry'); ui.state.expiresOn.value = '2028-01-01'; await ui.state.makePreview(); assert.equal(requests.length, 1)
  const html = await render(plan('bwh')); assert.match(html, /供应商官方重置（只读）/); assert.match(html, /采购到期日期/)
  ui.unmount()
})

test('关闭写入、失权与切换资源迟到响应均不能伪成功', async () => {
  let resolve!: (value: any) => void, posts = 0
  const ui = mount(async (path, method, body) => {
    if (method !== 'POST') return { ...plan(), meter_id: path.split('/').at(-1) }
    posts++; if (path.endsWith('/preview')) return previewResult(body)
    return new Promise(r => { resolve = r })
  }, false)
  await flush(); await ui.state.makePreview(); assert.equal(posts, 0)
  ui.props.enabled = true; await flush(); await ui.state.makePreview(); const confirming = ui.state.confirm()
  ui.props.bindingKey = 'other-resource'; await flush(); resolve({ ...plan(), receipt: { id: 'old' } }); await confirming
  assert.equal(ui.events.length, 0); assert.equal(ui.state.current.value.meter_id, 'other-resource'); ui.unmount()
  const denied = mount(async (_path, method) => { if (method !== 'POST') return plan(); throw new ApiError(403, 'permission_denied', '无权操作') })
  await flush(); await denied.state.makePreview(); assert.equal(denied.state.current.value, null); assert.equal(denied.events.length, 0); denied.unmount()
})

test('保存回执与实际消费明确区分，读取状态不伪造消费时间', async () => {
  let base = plan()
  base.consumption = { status: 'pending', revision: null, consumed_at: null }
  let html = await render(base); assert.match(html, /等待生效/); assert.doesNotMatch(html, /已生效/)
  base.consumption = { status: 'consumed', revision: 1, consumed_at: '2026-10-18T11:30:00Z' }
  html = await render(base); assert.match(html, /版本 1 已生效/); assert.match(html, /19:30/)
  base.consumption.revision = 0; html = await render(base); assert.doesNotMatch(html, /已生效/)
})

test('非法额度与周期不请求后端；只读刷新保留未保存输入', async () => {
  let posts = 0
  const ui = mount(async (_path, method) => { if (method === 'POST') posts++; return plan() })
  await flush(); ui.state.quotaGB.value = '-1'; await ui.state.makePreview(); assert.equal(posts, 0)
  ui.state.quotaGB.value = '333'; await ui.state.load(); assert.equal(ui.state.quotaGB.value, '333')
  ui.state.changeOperation('cycle'); ui.state.cycleDays.value = '0'; ui.state.nextReset.value = '2026-11-18T00:00'; await ui.state.makePreview(); assert.equal(posts, 0)
  ui.unmount()
})

test('消费拒绝提示重选重置时间，目标值不冒充运行值，也不无限等待', async () => {
  const base = plan(); base.revision = 3
  base.consumption = { status: 'rejected', revision: 3, consumed_at: '2026-10-18T11:30:00Z', error_code: 'cycle_reset_expired', effective_plan_fingerprint: 'b'.repeat(64) }
  const html = await render(base)
  assert.match(html, /重置时间已过，请重新选择/); assert.match(html, /当前有效计划/)
  assert.match(html, /原有效计划与统计保持不变/); assert.match(html, /下方重选目标周期/)
  assert.doesNotMatch(html, /尚待采集流程读取|已生效/)
})

test('拒绝回执结束原保存请求，可显式重选未来周期并产生新预览', async () => {
  const rejected = plan(); rejected.revision = 2
  rejected.consumption = { status: 'rejected', revision: 2, consumed_at: '2026-10-18T11:30:00Z', error_code: 'cycle_reset_expired' }
  let previews = 0
  const ui = mount(async (path, method, body) => {
    if (method !== 'POST') return plan()
    if (path.endsWith('/preview')) { previews++; return previewResult(body, { ...plan(), revision: body.expected_revision }) }
    return { ...rejected, receipt: { id: 'rejected-receipt', revision: 2, status: 'rejected', error_code: 'cycle_reset_expired', replayed: true } }
  })
  await flush(); ui.state.changeOperation('cycle'); ui.state.cycleDays.value = '31'; ui.state.nextReset.value = '2026-11-19T00:00'
  await ui.state.makePreview(); await ui.state.confirm()
  assert.equal(ui.state.receipt.value.status, 'rejected'); assert.equal(ui.state.pending.value, null); assert.equal(ui.state.error.value, '')
  ui.state.nextReset.value = '2020-01-01T00:00'; ui.state.recoverCycle(); assert.equal(ui.state.nextReset.value, '')
  ui.state.nextReset.value = '2028-01-20T00:00'; ui.state.recoverCycle(); assert.equal(ui.state.nextReset.value, '2028-01-20T00:00')
  await ui.state.makePreview(); assert.equal(previews, 2); assert.equal(ui.state.preview.value.revision, 2)
  assert.deepEqual(ui.state.preview.value.changes, { days: 31, next_reset_at: '2028-01-20T00:00:00+08:00' })
  assert.equal(ui.state.current.value.consumption.status, 'rejected'); assert.equal(ui.state.current.value.plan.quota_bytes, '400000000000')
  ui.unmount()
})

test('拒绝周期后额度消费成功，当前有效区仍保原周期，目标值仅供编辑预览', async () => {
  const base = plan(); base.revision = 3
  base.plan.cycle = { kind: 'fixed_days', days: 31, anchor: '2026-10-17T00:00:00+08:00' }
  base.plan.quota_bytes = '500000000000'; base.effective_plan.quota_bytes = '500000000000'
  base.consumption = { status: 'consumed', revision: 3, consumed_at: '2026-10-18T11:30:00Z' }
  const html = await render(base)
  const effective = html.match(/<dl[^>]+aria-label="当前有效采购计划"[\s\S]*?<\/dl>/)?.[0] || ''
  assert.match(effective, /500 GB/); assert.match(effective, /每 30 天/); assert.doesNotMatch(effective, /每 31 天/)
  assert.match(html, /版本 3 已生效/)
  const ui = mount(async (_path, method, body) => method === 'POST' ? previewResult(body, base) : base)
  await flush(); ui.state.changeOperation('quota'); ui.state.quotaGB.value = '600'; await ui.state.makePreview()
  assert.equal(ui.state.preview.value.before.cycle.days, 31)
  assert.equal(ui.state.current.value.effective_plan.cycle.days, 30)
  ui.unmount()
})

test('有效计划缺失时不使用目标额度或周期伪装实际值', async () => {
  const base = plan(); delete base.effective_plan
  const html = await render(base)
  assert.match(html, /当前有效计划暂不可读取/); assert.doesNotMatch(html, /aria-label="当前有效采购计划"/)
})

test('读取新版本撤下旧差异预览，但保留未保存目标输入', async () => {
  let revision = 1
  const ui = mount(async (_path, method, body) => method === 'POST' ? previewResult(body) : { ...plan(), revision: revision++ })
  await flush(); ui.state.quotaGB.value = '600'; await ui.state.makePreview(); assert.ok(ui.state.preview.value)
  await ui.state.load(); assert.equal(ui.state.preview.value, null); assert.equal(ui.state.quotaGB.value, '600')
  ui.unmount()
})
