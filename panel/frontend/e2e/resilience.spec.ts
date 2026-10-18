import { expect, type Page, type Route } from '@playwright/test'
import { test, scenario, save, fixture, login, noOverflow, fixtureControl, networkEvidence } from './helpers'

const api = '/api/v1'
const refresh = (page: Page) => page.locator('main .page-title .refresh-control')
const userService = fixture.service_ids['fixture-user']
const adminService = fixture.service_ids['fixture-admin']
async function go(page: Page, path: string) { await page.goto('/app/#' + path) }
async function logout(page: Page) {
  await page.locator('.shell-account-menu summary').click()
  await page.getByRole('button', { name: '退出登录', exact: true }).click()
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible()
}
async function radio(page: Page, name: string) {
  await page.locator('label').filter({ has: page.getByRole('radio', { name, exact: true }) }).click()
}
// 只记录本地接口路径、方法及状态；不保存请求体、Cookie 或下载链接。
async function hold(page: Page, pattern: string | RegExp) {
  let release!: () => void, arrived!: () => void, settled!: () => void
  let count = 0
  const ready = new Promise<void>(resolve => { arrived = resolve })
  const gate = new Promise<void>(resolve => { release = resolve })
  const finished = new Promise<void>(resolve => { settled = resolve })
  const handler = async (route: Route) => {
    count++
    const response = await route.fetch()
    arrived()
    await gate
    await route.fulfill({ response }).catch(() => {})
    settled()
  }
  await page.route(pattern, handler)
  return { ready, count: () => count, release: async () => { release(); await finished; await page.unroute(pattern, handler) } }
}

test('CP08 权限、跨用户与迟到响应隔离 @desktop', async ({ page }, info) => {
  await page.clock.setFixedTime(new Date(fixture.fixed_time))
  const cp = scenario('CP08', info), network = networkEvidence(page, info, 'CP08')
  try {
    await cp.check('CP08-A01', async () => {
      for (const path of ['/services', '/guides', '/admin/servers']) {
        await go(page, path)
        await expect(page.getByLabel('账号', { exact: true })).toBeVisible()
        expect((await page.request.get(api + (path === '/guides' ? '/me/services' : path === '/services' ? '/me/services' : '/admin/servers'))).status()).toBe(401)
      }
      await login(page, 'fixture-user')
      expect((await page.request.get(api + '/admin/servers')).status()).toBe(403)
      await go(page, '/admin/servers')
      await expect(page.locator('.server-grid')).toHaveCount(0)
      await expect(page.locator('.shell-account-menu')).not.toContainText('管理面板')
    })
    await cp.check('CP08-A02', async () => {
      expect((await page.request.get(api + '/me/services/' + adminService)).status()).toBe(404)
      await go(page, '/services/' + adminService)
      await expect(page.locator('.el-alert')).toBeVisible()
      await expect(page.locator('[data-chart="quota-gauge"]')).toHaveCount(0)
      await expect(page.getByRole('tab', { name: '连接设置' })).toHaveCount(0)
      await cp.shot(page, 'permission-denied')
      await logout(page); await login(page)
      const body = await (await page.request.get(api + '/me/services/' + adminService)).json()
      for (const key of ['password', 'credentials', 'upstream_password', 'raw_config', 'private_key', 'purchase_meter']) expect(body.data).not.toHaveProperty(key)
      await go(page, '/services/' + adminService)
      await expect(page.locator('[data-chart="quota-gauge"]')).toBeVisible()
      await expect(page.locator('main')).not.toContainText('采购预算')
    })
    await cp.check('CP08-A03', async () => {
      const held = await hold(page, '**/api/v1/me/services/' + adminService)
      await refresh(page).getByRole('button').click(); await held.ready
      await logout(page); await login(page, 'fixture-empty')
      await expect(page.getByRole('heading', { name: '暂时还没有服务' })).toBeVisible()
      await held.release()
      await expect(page.locator('.service-card')).toHaveCount(0)
      await expect(page.locator('[data-chart="quota-gauge"]')).toHaveCount(0)
      await logout(page); await login(page, 'fixture-user')
      await go(page, '/services/' + userService)
      await expect(page.locator('[data-chart="quota-gauge"]')).toBeVisible()
      await page.route('**/api/v1/me/services/' + userService, route => route.fulfill({ status: 403, json: { error: { code: 'permission_denied', message: '此服务授权已撤销' } } }))
      await refresh(page).getByRole('button').click()
      await expect(page.locator('[data-chart="quota-gauge"]')).toHaveCount(0)
      await expect(page.getByRole('tab', { name: '连接设置' })).toHaveCount(0)
      await expect(page.locator('main')).toContainText('此服务授权已撤销')
      await page.unroute('**/api/v1/me/services/' + userService)
    })
    cp.finish()
  } finally { cp.result.network_log = network(); save(info) }
})

test('CP09 刷新等待失败保留与恢复', async ({ page }, info) => {
  await page.clock.setFixedTime(new Date(fixture.fixed_time))
  const cp = scenario('CP09', info), network = networkEvidence(page, info, 'CP09')
  let mutations = 0
  page.on('request', r => { if (r.method() !== 'GET' && /resource-checks|collect|refresh-provider/.test(r.url())) mutations++ })
  try {
    await login(page)
    await go(page, '/services/' + adminService)
    await expect(page.locator('[data-chart="quota-gauge"]')).toBeVisible()
    await expect(refresh(page)).toContainText('已读取最新可用数据')
    const original = await page.locator('.quota-legend').innerText()
    const sampled = await page.getByTestId('usage-updated-at').innerText()
    await cp.check('CP09-A01', async () => {
      await expect(page.locator('.refresh-control')).toHaveCount(1)
      await expect(refresh(page)).toContainText('已读取最新可用数据')
      const held = await hold(page, '**/api/v1/me/services/' + adminService)
      await refresh(page).getByRole('button').click(); await held.ready
      await expect(refresh(page)).toContainText('正在读取已采集数据')
      await expect(page.locator('[data-chart="quota-gauge"]')).toBeVisible()
      await expect(page.locator('.el-skeleton:visible')).toHaveCount(0)
      await cp.shot(page, 'refresh-pending')
      await held.release()
      await expect(refresh(page)).toContainText('已读取最新可用数据')
    })
    await cp.check('CP09-A02', async () => {
      const url = '**/api/v1/me/services/' + adminService
      await page.route(url, r => r.fulfill({ status: 503, json: { error: { code: 'fixture_unavailable', message: '读取暂不可用' } } }))
      await refresh(page).getByRole('button').click()
      await expect(refresh(page)).toContainText('读取失败，已保留上次内容')
      await expect(page.locator('.quota-legend')).toHaveText(original, { useInnerText: true })
      await expect(page.getByTestId('usage-updated-at')).toHaveText(sampled, { useInnerText: true })
      await expect(page.locator('[data-chart="transfer-composition"]')).toBeVisible()
      await cp.shot(page, 'refresh-failed')
      await page.getByRole('tab', { name: '连接设置', exact: true }).click()
      await page.getByRole('button', { name: /电脑 Windows/ }).click(); await radio(page, 'Windows'); await radio(page, 'v2rayN')
      await refresh(page).getByRole('button').click()
      await expect(page.getByRole('heading', { name: 'v2rayN 连接设置' })).toBeVisible()
      await page.getByRole('tab', { name: '流量用量', exact: true }).click()
      await expect(page.locator('[data-chart="usage-timeseries"]')).toBeVisible()
      await page.evaluate(() => window.scrollTo(0, 200))
      const scroll = await page.evaluate(() => window.scrollY)
      // DOM click 避免测试工具自动滚动按钮，真实事件仍由已挂载组件处理。
      await refresh(page).getByRole('button').evaluate((el: HTMLButtonElement) => el.click())
      await expect(refresh(page)).toContainText('读取失败，已保留上次内容')
      expect(await page.evaluate(() => window.scrollY)).toBe(scroll)
      await expect(page.locator('[data-chart="usage-timeseries"]')).toBeVisible()
      await page.unroute(url)
    })
    await cp.check('CP09-A03', async () => {
      const held = await hold(page, '**/api/v1/me/services/' + adminService)
      await refresh(page).getByRole('button').click(); await held.ready
      await refresh(page).getByRole('button').evaluate((el: HTMLButtonElement) => { el.click(); el.click() })
      expect(held.count()).toBe(1)
      await held.release(); await expect(refresh(page)).toContainText('已读取最新可用数据')
      await expect(refresh(page)).not.toHaveClass(/has-error/)
      const oldRange = await hold(page, '**/api/v1/me/services/' + adminService + '/usage?period=month')
      await radio(page, '本月'); await oldRange.ready
      await radio(page, '今年')
      await expect(page.locator('.timeline .chart-column')).toHaveCount(12)
      await oldRange.release()
      await expect(page.getByRole('radio', { name: '今年', exact: true })).toBeChecked()
      await expect(page.locator('.timeline .chart-column')).toHaveCount(12)
    })
    await cp.check('CP09-A04', async () => {
      await go(page, '/admin')
      await expect(page.getByRole('heading', { name: '管理总览', exact: true })).toBeVisible()
      await expect(page.locator('.refresh-control')).toHaveCount(1)
      const metrics = page.locator('.overview-metrics').first()
      await expect(metrics).toBeVisible()
      const text = await metrics.innerText()
      const pending = await hold(page, '**/api/v1/admin/overview')
      await refresh(page).getByRole('button').click(); await pending.ready
      await expect(metrics).toHaveText(text, { useInnerText: true }); await pending.release()
      await expect(refresh(page)).toContainText('已读取最新可用数据')
      await page.route('**/api/v1/admin/overview', r => r.fulfill({ status: 503, json: { error: { code: 'fixture_unavailable', message: '总览暂不可用' } } }))
      await refresh(page).getByRole('button').click()
      await expect(refresh(page)).toContainText('读取失败，已保留上次内容')
      await expect(metrics).toHaveText(text, { useInnerText: true })
      await page.unroute('**/api/v1/admin/overview')
      await refresh(page).getByRole('button').click()
      await expect(refresh(page)).toContainText('已读取最新可用数据')
      await go(page, '/admin/servers')
      await expect(page.locator('.server-grid')).toContainText('21.5')
      const cards = await page.locator('.server-grid').innerText()
      const held = await hold(page, /\/api\/v1\/admin\/servers(?:\?.*)?$/)
      await refresh(page).getByRole('button').click(); await held.ready
      await expect(page.locator('.server-grid')).toHaveText(cards, { useInnerText: true }); await held.release()
      await page.getByRole('button', { name: '验收服务器', exact: true }).click()
      const drawer = page.locator('.el-drawer')
      await expect(drawer.locator('[aria-label="服务器概览"]')).toContainText('21.5')
      const time = await drawer.locator('.refresh-control time').last().getAttribute('datetime')
      await drawer.getByRole('button', { name: '核心', exact: true }).click()
      const detailUrl = '**/api/v1/admin/servers/' + fixture.server_id
      await page.route(detailUrl, r => r.fulfill({ status: 503, json: { error: { code: 'fixture_unavailable', message: '详情暂不可用' } } }))
      await drawer.locator('.refresh-control button').click()
      await expect(drawer.locator('.refresh-control')).toContainText('读取失败，已保留上次内容')
      await expect(drawer.getByRole('button', { name: '核心', exact: true })).toHaveAttribute('aria-current', 'page')
      await expect(drawer).toContainText('验收 sing-box 核心')
      expect(await drawer.locator('.refresh-control time').last().getAttribute('datetime')).toBe(time)
      await page.unroute(detailUrl)
      await drawer.locator('.refresh-control button').click()
      await expect(drawer.locator('.refresh-control')).toContainText('已读取最新可用数据')
      expect(mutations).toBe(0); await noOverflow(page)
    })
    cp.finish()
  } finally { cp.result.network_log = network(); save(info) }
})

test('CP10 固定检查提交读取截止与同任务恢复 @desktop', async ({ page }, info) => {
  await page.clock.setFixedTime(new Date(fixture.fixed_time))
  const cp = scenario('CP10', info), network = networkEvidence(page, info, 'CP10')
  fixtureControl('reset_checks')
  const keys: string[] = []
  let reads = 0
  page.on('request', r => {
    if (r.url().endsWith('/api/v1/admin/resource-checks') && r.method() === 'POST') keys.push(r.postDataJSON().idempotency_key)
    if (r.url().includes('/api/v1/admin/resource-checks/') && r.method() === 'GET') reads++
  })
  const checks = page.locator('.el-drawer .inventory-checks')
  const open = async () => {
    await go(page, '/admin/servers?server_id=' + fixture.server_id + '&section=checks')
    await expect(checks.getByRole('heading', { name: '发起固定检查' })).toBeVisible()
  }
  let jobId = '', postElapsed = 0, getElapsed = 0
  try {
    await login(page); await open()
    const post = await hold(page, '**/api/v1/admin/resource-checks')
    const start = Date.now()
    await checks.locator('.check-actions button').first().click(); await post.ready
    await expect(checks.getByRole('status')).toContainText('提交等待已超过15秒', { timeout: 18_000 })
    postElapsed = Date.now() - start
    expect(postElapsed).toBeGreaterThanOrEqual(14_500); expect(postElapsed).toBeLessThan(19_000)
    await expect(checks.locator('.check-actions button').first()).toBeEnabled()
    await expect(checks).toContainText('结果未知')
    await cp.shot(page, 'check-timeout')
    await post.release()
    await cp.check('CP10-A02', async () => {
      const before = fixtureControl('inspect_jobs').jobs
      expect(before).toHaveLength(1); jobId = before[0].public_id
      await checks.locator('.check-actions button').first().click()
      await expect(checks.locator('.job-status')).toContainText(jobId)
      const after = fixtureControl('inspect_jobs').jobs
      expect(keys).toHaveLength(2); expect(keys[0]).toBe(keys[1])
      expect(after).toHaveLength(1); expect(after[0].public_id).toBe(jobId)
      await expect(checks.locator('.job-status')).not.toContainText('本层通过')
    })
    const get = await hold(page, '**/api/v1/admin/resource-checks/' + jobId)
    const readStart = Date.now()
    await checks.getByRole('button', { name: '读取任务状态', exact: true }).click(); await get.ready
    await cp.check('CP10-A01', async () => {
      await expect(checks.getByRole('status')).toContainText('读取等待已超过15秒', { timeout: 18_000 })
      getElapsed = Date.now() - readStart
      expect(getElapsed).toBeGreaterThanOrEqual(14_500); expect(getElapsed).toBeLessThan(19_000)
      await expect(checks.getByRole('button', { name: '读取任务状态', exact: true })).toBeEnabled()
      await expect(checks.locator('.job-status')).toContainText(jobId)
      expect(keys).toHaveLength(2)
      const stopped = reads
      await page.waitForTimeout(1800)
      expect(reads).toBe(stopped)
      cp.result.deadlines = { post_elapsed_ms: postElapsed, get_elapsed_ms: getElapsed, public_limit_ms: 15000 }
    })
    fixtureControl('complete_job', jobId)
    await get.release()
    await cp.check('CP10-A04', async () => {
      await checks.getByRole('button', { name: '读取任务状态', exact: true }).click()
      await expect(checks.locator('.job-status')).toContainText('检查已结束')
      await expect(checks.locator('.job-status')).toContainText(jobId)
      await expect(checks.locator('.job-status')).toContainText('目标版本')
      await expect(checks.locator('.job-status')).toContainText('2026')
      await expect(checks.locator('.job-status')).toContainText('TCP连接')
      await expect(checks.locator('.job-status')).toContainText('本层通过')
      await expect(checks).toContainText('不测试认证、DNS、UDP、出口与整链')
      await expect(checks).toContainText('NOT TESTED')
      await cp.shot(page, 'check-recovered')
    })
    await cp.check('CP10-A03', async () => {
      fixtureControl('reset_checks')
      await go(page, '/services'); await open()
      const late = await hold(page, '**/api/v1/admin/resource-checks')
      await checks.locator('.check-actions button').first().click(); await late.ready
      const detailUrl = '**/api/v1/admin/servers/' + fixture.server_id
      await page.route(detailUrl, async route => {
        const response = await route.fetch(), body = await response.json()
        for (const action of body.data.check_actions) action.target_version = 'fixture-new-version'
        await route.fulfill({ response, json: body })
      })
      await page.locator('.el-drawer .refresh-control button').click()
      await expect(checks.locator('.check-actions button').first()).toBeEnabled()
      await late.release()
      await expect(checks.locator('.job-status')).toHaveCount(0)
      await page.unroute(detailUrl)
      await go(page, '/services')
      await expect(page.locator('.service-card')).toHaveCount(1)
      await expect(page.locator('.job-status')).toHaveCount(0)
      fixtureControl('reset_checks'); await open()
      await checks.locator('.check-actions button').first().click()
      await expect(checks.locator('.job-status')).toBeVisible()
      const abandoned = fixtureControl('inspect_jobs').jobs[0].public_id
      const leave = await hold(page, '**/api/v1/admin/resource-checks/' + abandoned)
      await checks.getByRole('button', { name: '读取任务状态', exact: true }).click(); await leave.ready
      await go(page, '/services'); await leave.release()
      await expect(page.locator('.service-card')).toHaveCount(1)
      await expect(page.locator('.job-status')).toHaveCount(0)
      const abandonedReads = reads
      await page.waitForTimeout(1800)
      expect(reads).toBe(abandonedReads)
      fixtureControl('reset_checks'); await open()
      await checks.locator('.check-actions button').first().click()
      await expect(checks.locator('.job-status')).toBeVisible()
      fixtureControl('revoke_admin')
      await checks.getByRole('button', { name: '读取任务状态', exact: true }).click()
      await expect(checks.locator('.job-status')).toHaveCount(0)
      await expect(checks.locator('.check-actions button').first()).toBeDisabled()
      const requests = keys.length, stopped = reads
      await page.waitForTimeout(1800)
      expect(keys).toHaveLength(requests)
      expect(reads).toBe(stopped)
      expect((await page.request.get(api + '/admin/resource-checks/' + fixtureControl('inspect_jobs').jobs[0].public_id)).status()).toBe(403)
    })
    cp.finish()
  } finally { fixtureControl('restore_admin'); cp.result.network_log = network(); save(info) }
})
