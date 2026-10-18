import { expect, type Locator, type Page, type Route, type TestInfo } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'
import { fixture, fixtureControl, hash, login, noOverflow, run, test } from './helpers'

type M1Fixture = { server_id: string; core_id: string; egress_id: number; protected_sha256: string }
let current: M1Fixture
let checkpoints: { checkpoint: string; path: string; sha256: string }[] = []
let network: { path: string; method: string; status: number }[] = []

function recordNetwork(page: Page) {
  page.on('response', response => {
    const url = new URL(response.url())
    if (url.pathname.startsWith('/api/')) network.push({ path: url.pathname,
      method: response.request().method(), status: response.status() })
  })
}

function control(operation: string, jobId?: string) {
  const args = [fileURLToPath(new URL('./core_resource_fixture.py', import.meta.url)),
    operation === 'prepare' ? 'prepare' : 'control', '--run', run]
  if (operation !== 'prepare') args.push('--operation', operation)
  if (jobId) args.push('--job-id', jobId)
  return JSON.parse(execFileSync(process.env.ACCEPTANCE_PYTHON!, args, { encoding: 'utf8' }))
}

async function shot(page: Page, info: TestInfo, checkpoint: string) {
  await noOverflow(page)
  await page.evaluate(() => document.fonts.ready)
  const path = `screenshots/${info.project.name}/M1-${checkpoint}.png`
  mkdirSync(join(run, 'screenshots', info.project.name), { recursive: true })
  await page.mouse.move(0, 0)
  let previous: Buffer | undefined, bytes: Buffer | undefined
  for (let attempt = 0; attempt < 20; attempt++) {
    const next = await page.screenshot({ animations: 'disabled' })
    if (previous?.equals(next)) { bytes = next; break }
    previous = next
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  if (!bytes) throw new Error(`M1 页面未稳定：${checkpoint}`)
  writeFileSync(join(run, path), bytes)
  checkpoints.push({ checkpoint, path, sha256: hash(bytes) })
  // bootstrap 只留待审证据；正常门禁必须使用主代理手审接收的基线。
  await info.attach(checkpoint, { path: join(run, path), contentType: 'image/png' })
  if (process.env.ACCEPTANCE_BOOTSTRAP !== '1') {
    const name = `M1-${checkpoint}.png`
    if (!existsSync(info.snapshotPath(name))) throw new Error(`缺少已审视觉基线：${info.snapshotPath(name)}`)
    await expect(page).toHaveScreenshot(name, { animations: 'disabled', maxDiffPixels: 0 })
  }
}

async function holdResponse(page: Page, path: string, method: string) {
  let release!: () => void, arrived!: () => void, settled!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  const ready = new Promise<void>(resolve => { arrived = resolve })
  const finished = new Promise<void>(resolve => { settled = resolve })
  const handler = async (route: Route) => {
    if (route.request().method() !== method) return route.fallback()
    const response = await route.fetch()
    arrived()
    await gate
    try { await route.fulfill({ response }) }
    finally { settled() }
  }
  await page.route('**' + path, handler)
  return { ready, release: async () => { release(); await finished; await page.unroute('**' + path, handler) } }
}

async function openNode(page: Page) {
  await page.goto(`/app/#/admin/servers?server_id=${current.server_id}&section=nodes`)
  const node = page.locator('.node-entry').filter({ hasText: 'm1-home.example.invalid' })
  await node.locator(':scope > summary').click()
  const editor = node.locator('[aria-label="资源资料编辑"]')
  await expect(editor).toBeVisible()
  await editor.getByRole('button', { name: '编辑资料', exact: true }).click()
  await expect(editor.getByLabel('资源别名', { exact: true })).toBeVisible()
  return editor
}

async function previewMetadata(editor: Locator) {
  await editor.getByRole('button', { name: '预览修改', exact: true }).click()
  await expect(editor.getByRole('button', { name: '确认保存资料', exact: true })).toBeEnabled()
}

test.beforeEach(async ({ page }) => {
  fixtureControl('usage-rate-reset')
  current = control('prepare')
  checkpoints = []; network = []
  recordNetwork(page)
  await page.clock.setFixedTime(new Date(fixture.fixed_time))
})

test.afterEach(async ({}, info) => {
  let cleanup: unknown
  try {
    cleanup = control('cleanup')
  } finally {
    const id = info.title.split(' ')[0]
    const assertionId = ({ 'M1-01': 'M1A01', 'M1-02': 'M1A02', 'M1-03': 'M1A03', 'M1-04': 'M1A04' } as Record<string, string>)[id]
    const passed = info.status === 'passed' && !!cleanup
    writeFileSync(join(run, `results-m1-${info.project.name}-${id}.json`), JSON.stringify([{
      scenario_id: id, viewport: info.project.name,
      status: passed ? 'PASS' : 'FAIL',
      assertions: passed ? [{ id: assertionId, status: 'PASS' }] : [],
      ...(process.env.ACCEPTANCE_BOOTSTRAP === '1' ? { visual_status: 'PENDING_REVIEW' } : {}), screenshots: checkpoints,
      execution_boundary: 'real-django-compiled-ui-with-fake-core-executor',
      network, protected_business_cleanup: cleanup || 'NOT VERIFIED',
    }], null, 2))
  }
})

test('M1-01 节点资料保存等待、真实并发冲突与重新进入', async ({ page, browser }, info) => {
  await login(page)
  const editor = await openNode(page)
  const context = await browser.newContext({ viewport: info.project.use.viewport,
    locale: 'zh-CN', timezoneId: 'Asia/Shanghai', baseURL: process.env.ACCEPTANCE_BASE_URL })
  try {
    const other = await context.newPage()
    recordNetwork(other)
    await other.clock.setFixedTime(new Date(fixture.fixed_time))
    await login(other)
    const staleEditor = await openNode(other)
    await editor.getByLabel('资源别名', { exact: true }).fill('M1 外部节点已保存')
    await staleEditor.getByLabel('资源别名', { exact: true }).fill('M1 另一窗口的输入')
    await previewMetadata(editor)
    await previewMetadata(staleEditor)
    const path = `/api/v1/admin/egresses/${current.egress_id}`
    const held = await holdResponse(page, path, 'PATCH')
    try {
      await editor.getByRole('button', { name: '确认保存资料', exact: true }).click()
      await held.ready
      await expect(editor.getByLabel('资源别名', { exact: true })).toHaveValue('M1 外部节点已保存')
      await expect(editor.getByRole('button', { name: '重试原保存请求', exact: true })).toBeDisabled()
      await shot(page, info, 'metadata-saving')
    } finally { await held.release() }
    await expect(editor.getByRole('status')).toContainText('资料已保存')
    const conflictResponse = other.waitForResponse(response => response.url().endsWith(path) && response.request().method() === 'PATCH')
    await staleEditor.getByRole('button', { name: '确认保存资料', exact: true }).click()
    expect((await conflictResponse).status()).toBe(409)
    await expect(staleEditor.getByLabel('资源别名', { exact: true })).toHaveValue('M1 另一窗口的输入')
    await staleEditor.getByRole('button', { name: '读取当前版本，保留输入', exact: true }).click()
    await expect(staleEditor).toContainText('M1 外部节点已保存')
    await expect(staleEditor.getByLabel('资源别名', { exact: true })).toHaveValue('M1 另一窗口的输入')
    await shot(other, info, 'metadata-conflict')
    await staleEditor.getByRole('button', { name: '核对后采用最新版本，保留我的输入', exact: true }).click()
    await previewMetadata(staleEditor)
    await staleEditor.getByRole('button', { name: '确认保存资料', exact: true }).click()
    await expect(staleEditor.getByRole('status')).toContainText('资料已保存')
    await other.reload()
    const savedNode = other.locator('.node-entry').filter({ hasText: 'M1 另一窗口的输入' })
    await savedNode.locator(':scope > summary').click()
    await savedNode.getByRole('button', { name: '编辑资料', exact: true }).click()
    await expect(savedNode.getByLabel('资源别名', { exact: true })).toHaveValue('M1 另一窗口的输入')
    const detail = await (await other.request.get(path)).json()
    expect(detail.data.item.name).toBe('M1 另一窗口的输入')
    expect(control('inspect').protected_sha256).toBe(current.protected_sha256)
    await shot(other, info, 'metadata-reentered')
  } finally { await context.close() }
})

test('M1-02 核心检查失败、UNKNOWN 阻断与只读恢复', async ({ page }, info) => {
  await login(page)
  await page.goto(`/app/#/admin/servers?server_id=${current.server_id}&section=cores`)
  await page.getByRole('button', { name: /M1 隔离核心/ }).click()
  const panel = page.locator('[aria-label="核心配置控制"]')
  await expect(panel).toBeVisible()
  await panel.getByLabel('候选日志级别', { exact: true }).selectOption('warning')
  await panel.getByRole('button', { name: '生成候选', exact: true }).click()
  await expect(panel.locator('[aria-label="候选差异与影响"]')).toContainText('warning')
  await expect(panel.getByRole('button', { name: '应用候选', exact: true })).toBeDisabled()

  const queuedJob = async (action: 'check' | 'apply') => {
    const response = page.waitForResponse(item => item.request().method() === 'POST' && item.url().endsWith('/' + action))
    await panel.getByRole('button', { name: action === 'check' ? '检查候选' : '应用候选', exact: true }).click()
    const result = await response
    expect(result.status()).toBe(202)
    return (await result.json()).data.job.id as string
  }
  const refresh = async () => {
    await panel.getByRole('button', { name: '更新执行状态', exact: true }).click()
  }
  const failed = await queuedJob('check')
  await expect(panel.locator('[aria-label="核心执行任务"]')).toContainText('等待执行')
  await expect(panel.getByRole('button', { name: '检查候选', exact: true })).toBeDisabled()
  await expect(panel.getByRole('button', { name: '应用候选', exact: true })).toBeDisabled()
  await shot(page, info, 'core-check-pending')
  control('check-failed', failed); await refresh()
  await expect(panel.locator('[aria-label="核心执行任务"]')).toContainText(/失败/)
  await expect(panel.getByRole('button', { name: '应用候选', exact: true })).toBeDisabled()
  expect(control('inspect').configuration_version).toBe('a'.repeat(64))
  await shot(page, info, 'core-check-failed')

  const checked = await queuedJob('check')
  control('check-succeeded', checked); await refresh()
  await panel.getByRole('checkbox', { name: '我已核对本候选差异及连接中断影响', exact: true }).check()
  await expect(panel.getByRole('button', { name: '应用候选', exact: true })).toBeEnabled()
  const applied = await queuedJob('apply')
  control('apply-unknown', applied); await refresh()
  await expect(panel.locator('[aria-label="核心执行任务"]')).toContainText(/未知|待核对/)
  await expect(panel.getByRole('button', { name: '应用候选', exact: true })).toBeDisabled()
  await expect(panel.getByRole('button', { name: '回滚当前候选', exact: true })).toHaveCount(0)
  await expect(panel.getByRole('button', { name: /重试.*应用|重新执行/ })).toHaveCount(0)
  const before = control('inspect')
  await refresh()
  const readOnly = control('inspect')
  expect(readOnly.events).toEqual(before.events)
  expect(readOnly.jobs).toEqual(before.jobs)
  await shot(page, info, 'core-unknown')
  control('reconcile-succeeded', applied); await refresh()
  await expect(panel).toContainText('b'.repeat(64))
  const after = control('inspect')
  expect(after.events.filter((event: { action: string; operation: string }) => event.action === 'apply' && event.operation === 'execute')).toHaveLength(1)
  expect(after.events.filter((event: { operation: string }) => event.operation === 'read_receipt')).toHaveLength(1)
  expect(after.configuration_version).toBe('b'.repeat(64))
  expect(after.protected_sha256).toBe(current.protected_sha256)
  await shot(page, info, 'core-reconciled')
})

test('M1-03 采购预览冲突、额度续费周期独立保存及真实消费', async ({ page, browser }, info) => {
  const purchase = control('prepare-purchase')
  await login(page, 'fixture-p8')
  const openPlan = async (target: Page) => {
    await target.goto(`/app/#/admin/servers?server_id=${current.server_id}&section=nodes`)
    const node = target.locator('.node-entry').filter({ hasText: 'm1-home.example.invalid' })
    await node.locator(':scope > summary').click()
    const plan = node.locator('[aria-label="采购计划设置"]')
    await expect(plan.getByLabel('采购额度（GB）', { exact: true })).toHaveValue('400')
    await expect(node.locator('[data-chart="quota-gauge"]')).toBeVisible()
    await expect(node.locator('[data-chart="transfer-composition"]')).toBeVisible()
    return { node, plan }
  }
  const { node, plan } = await openPlan(page)
  const path = `/api/v1/admin/resource-plans/${purchase.binding_key}`
  const before = control('inspect-purchase')
  const context = await browser.newContext({ viewport: info.project.use.viewport,
    locale: 'zh-CN', timezoneId: 'Asia/Shanghai', baseURL: process.env.ACCEPTANCE_BASE_URL })
  try {
    const other = await context.newPage(); recordNetwork(other)
    await other.clock.setFixedTime(new Date(fixture.fixed_time)); await login(other, 'fixture-p8')
    const { plan: stalePlan } = await openPlan(other)
    await plan.getByLabel('采购额度（GB）', { exact: true }).fill('500')
    await stalePlan.getByLabel('采购额度（GB）', { exact: true }).fill('450')
    await plan.getByRole('button', { name: '预览变更', exact: true }).click()
    await expect(plan.locator('[aria-label="采购变更预览"]')).toContainText('500 GB')
    await stalePlan.getByRole('button', { name: '预览变更', exact: true }).click()
    await expect(stalePlan.locator('[aria-label="采购变更预览"]')).toContainText('450 GB')
    const held = await holdResponse(page, path + '/confirm', 'POST')
    try {
      await plan.getByRole('button', { name: '确认保存采购计划', exact: true }).click()
      await held.ready
      await expect(plan.getByRole('button', { name: '确认保存采购计划', exact: true })).toBeDisabled()
      await expect(plan.getByLabel('采购额度（GB）', { exact: true })).toHaveValue('500')
      await expect(node.locator('[data-chart="quota-gauge"]')).toBeVisible()
      await shot(page, info, 'purchase-saving')
    } finally { await held.release() }
    await expect(plan.locator('.consumption')).toContainText('等待生效')
    const pending = control('inspect-purchase')
    expect(pending.plan.plan.quota_bytes).toBe('500000000000')
    expect(pending.plan.consumption.status).toBe('pending')
    expect(pending.view).toEqual(before.view)
    const conflict = other.waitForResponse(response => response.url().endsWith(path + '/confirm') && response.request().method() === 'POST')
    await stalePlan.getByRole('button', { name: '确认保存采购计划', exact: true }).click()
    expect((await conflict).status()).toBe(409)
    await expect(stalePlan.getByLabel('采购额度（GB）', { exact: true })).toHaveValue('450')
    await stalePlan.getByRole('button', { name: '读取当前版本，保留输入', exact: true }).click()
    await expect(stalePlan.locator('.conflict')).toContainText('500 GB')
    await stalePlan.getByRole('button', { name: '采用当前版本并重新预览', exact: true }).click()
    await expect(stalePlan.getByLabel('采购额度（GB）', { exact: true })).toHaveValue('450')
    await expect(stalePlan.getByRole('button', { name: '预览变更', exact: true })).toBeEnabled()
    await shot(other, info, 'purchase-conflict-preserved')
  } finally { await context.close() }

  const quota = control('consume-purchase')
  expect(quota.plan.consumption).toMatchObject({ status: 'consumed', revision: 2 })
  expect(quota.view.meters[0]).toMatchObject({ quota_bytes: '500000000000', used_bytes: purchase.used_bytes,
    expires_on: purchase.plan.expires_on, cycle: before.view.meters[0].cycle })
  await plan.getByRole('button', { name: '读取计划状态', exact: true }).click()
  await expect(plan.locator('.consumption')).toContainText('版本 2 已生效')

  await plan.getByRole('button', { name: '到期与续费', exact: true }).click()
  await plan.getByLabel('采购到期日期', { exact: true }).fill('2028-01-19')
  await plan.getByRole('button', { name: '预览变更', exact: true }).click()
  await expect(plan.locator('[aria-label="采购变更预览"]')).toContainText('2028-01-19')
  await plan.getByRole('button', { name: '确认保存采购计划', exact: true }).click()
  await expect(plan.locator('.consumption')).toContainText('等待生效')
  const expiry = control('consume-purchase')
  expect(expiry.plan.consumption).toMatchObject({ status: 'consumed', revision: 3 })
  expect(expiry.view.meters[0]).toMatchObject({ quota_bytes: '500000000000', used_bytes: purchase.used_bytes,
    expires_on: '2028-01-19', cycle: before.view.meters[0].cycle })

  await plan.getByRole('button', { name: '重置周期', exact: true }).click()
  await plan.getByLabel('周期天数', { exact: true }).fill('31')
  await plan.getByLabel('下次重置时间（上海）', { exact: true }).fill('2026-11-20T00:00')
  await plan.getByRole('button', { name: '预览变更', exact: true }).click()
  await expect(plan.locator('[aria-label="采购变更预览"]')).toContainText('每 31 天')
  await plan.getByRole('button', { name: '确认保存采购计划', exact: true }).click()
  await expect(plan.locator('.consumption')).toContainText('等待生效')
  const cycle = control('consume-purchase')
  expect(cycle.plan.consumption).toMatchObject({ status: 'consumed', revision: 4 })
  expect(cycle.view.meters[0]).toMatchObject({ quota_bytes: '500000000000', used_bytes: purchase.used_bytes,
    expires_on: '2028-01-19', cycle: { starts_at: before.view.meters[0].cycle.starts_at,
      next_reset_at: '2026-11-19T16:00:00+00:00' } })
  expect(cycle.state.meters.home.baseline).toEqual(before.state.meters.home.baseline)
  expect(cycle.protected_sha256).toBe(current.protected_sha256)
  // 保存时仍在未来的重置点，在定时器实际消费前已经过去；拒绝不能污染有效周期。
  await plan.getByLabel('周期天数', { exact: true }).fill('32')
  await plan.getByLabel('下次重置时间（上海）', { exact: true }).fill('2026-10-19T00:00')
  await plan.getByRole('button', { name: '预览变更', exact: true }).click()
  await expect(plan.locator('[aria-label="采购变更预览"]')).toContainText('每 32 天')
  await plan.getByRole('button', { name: '确认保存采购计划', exact: true }).click()
  await expect(plan.locator('.consumption')).toContainText('等待生效')
  try {
    const delayed = '2026-10-19T16:00:01+00:00'
    fixtureControl('usage-clock', delayed)
    await page.clock.setFixedTime(new Date(delayed))
    const rejected = control('consume-purchase')
    expect(rejected.plan.consumption).toMatchObject({ status: 'rejected', revision: 5, error_code: 'cycle_reset_expired' })
    expect(rejected.plan.effective_plan).toEqual(cycle.plan.effective_plan)
    expect(rejected.view.meters[0]).toMatchObject({ quota_bytes: '500000000000', used_bytes: purchase.used_bytes,
      cycle: cycle.view.meters[0].cycle })
    const expiredSession = page.waitForResponse(response => response.url().endsWith(path) && response.request().method() === 'GET')
    await plan.getByRole('button', { name: '读取计划状态', exact: true }).click()
    expect((await expiredSession).status()).toBe(401)
    await expect(page.getByLabel('账号', { exact: true })).toBeVisible()
    // 时间推进也会正常使会话过期；重新认证后继续验采购拒绝，不改变真实会话期限。
    await login(page, 'fixture-p8')
    await page.goto(`/app/#/admin/servers?server_id=${current.server_id}&section=nodes`)
    await node.locator(':scope > summary').click()
    await expect(plan.locator('.consumption')).toContainText('重置时间已过，请重新选择。')
    await expect(plan.locator('[aria-label="当前有效采购计划"]')).toContainText('每 31 天')
    await expect(plan.getByRole('button', { name: '重新选择重置时间', exact: true })).toBeEnabled()
    await plan.getByRole('button', { name: '额度与追加额', exact: true }).click()
    await plan.getByLabel('采购额度（GB）', { exact: true }).fill('600')
    await plan.getByRole('button', { name: '预览变更', exact: true }).click()
    await expect(plan.locator('[aria-label="采购变更预览"]')).toContainText('600 GB')
    await plan.getByRole('button', { name: '确认保存采购计划', exact: true }).click()
    await expect(plan.locator('.consumption')).toContainText('等待生效')
    const recovered = control('consume-purchase')
    expect(recovered.plan.consumption).toMatchObject({ status: 'consumed', revision: 6 })
    expect(recovered.plan.effective_plan).toEqual({ ...cycle.plan.effective_plan, quota_bytes: '600000000000' })
    expect(recovered.view.meters[0]).toMatchObject({ quota_bytes: '600000000000', used_bytes: purchase.used_bytes,
      expires_on: '2028-01-19', cycle: cycle.view.meters[0].cycle })
    expect(recovered.state.meters.home.baseline).toEqual(before.state.meters.home.baseline)
    expect(recovered.protected_sha256).toBe(current.protected_sha256)
    await page.reload()
    await node.locator(':scope > summary').click()
    await expect(plan.locator('.consumption')).toContainText('版本 6 已生效')
    await expect(plan.locator('[aria-label="当前有效采购计划"]')).toContainText('每 31 天')
    await expect(node.locator('[data-chart="quota-gauge"]')).toBeVisible()
    await expect(node.locator('[data-chart="transfer-composition"]')).toBeVisible()
    await expect(node.locator('.resource-usage-card')).toContainText('2028-01-19')
  await plan.locator('[aria-label="当前有效采购计划"]').scrollIntoViewIfNeeded()
  await shot(page, info, 'purchase-consumed-reentered')
  } finally { fixtureControl('usage-clock', fixture.fixed_time) }
})

test('M1-04 资料保存响应丢失复用原请求、撤权后拒绝写入', async ({ page }, info) => {
  await login(page)
  const editor = await openNode(page)
  const path = `/api/v1/admin/egresses/${current.egress_id}`
  const keys: string[] = []
  page.on('request', request => {
    if (request.url().endsWith(path) && request.method() === 'PATCH') keys.push(request.postDataJSON().idempotency_key)
  })
  await editor.getByLabel('资源别名', { exact: true }).fill('M1 回执丢失后恢复')
  await previewMetadata(editor)
  const loseResponse = async (route: Route) => {
    if (route.request().method() !== 'PATCH') return route.fallback()
    expect((await route.fetch()).status()).toBe(200)
    await route.abort('connectionreset')
  }
  await page.route('**' + path, loseResponse)
  await editor.getByRole('button', { name: '确认保存资料', exact: true }).click()
  await expect(editor.getByRole('button', { name: '重试原保存请求', exact: true })).toBeEnabled()
  await page.unroute('**' + path, loseResponse)
  await expect(editor.getByLabel('资源别名', { exact: true })).toHaveValue('M1 回执丢失后恢复')
  const savedRevision = control('inspect').node.revision
  const replay = page.waitForResponse(response => response.url().endsWith(path) && response.request().method() === 'PATCH')
  await editor.getByRole('button', { name: '重试原保存请求', exact: true }).click()
  expect((await (await replay).json()).data.replayed).toBe(true)
  await expect(editor.getByRole('status')).toContainText(/资料已保存|此前修改已保存/)
  expect(keys).toHaveLength(2)
  expect(keys[1]).toBe(keys[0])
  expect(control('inspect').node.revision).toBe(savedRevision)
  await shot(page, info, 'metadata-replayed')

  await editor.getByLabel('资源别名', { exact: true }).fill('M1 撤权后不得保存')
  await previewMetadata(editor)
  fixtureControl('revoke_admin')
  try {
    const denied = page.waitForResponse(response => response.url().endsWith(path) && response.request().method() === 'PATCH')
    await editor.getByRole('button', { name: '确认保存资料', exact: true }).click()
    expect((await denied).status()).toBe(403)
    expect(control('inspect').node).toEqual({ name: 'M1 回执丢失后恢复', revision: savedRevision })
    expect((await page.request.get(`/api/v1/admin/cores/${current.core_id}/control`)).status()).toBe(403)
    expect(control('inspect').protected_sha256).toBe(current.protected_sha256)
    await shot(page, info, 'metadata-permission-revoked')
  } finally { fixtureControl('restore_admin') }
})
