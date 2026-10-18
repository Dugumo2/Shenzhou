import { expect, type Page, type Route, type Browser } from '@playwright/test'
import { test, scenario, save, fixture, login, adminNavigation, noOverflow, fixtureControl, networkEvidence } from './helpers'

const adminId = fixture.service_ids['fixture-admin']
const userId = fixture.service_ids['fixture-user']
const usagePath = (id: string, period: string) => `/api/v1/me/services/${id}/usage?period=${period}`
const choose = async (page: Page, name: string) => page.locator('label').filter({ has: page.getByRole('radio', { name, exact: true }) }).click()
const refresh = (page: Page) => page.locator('main .page-title .refresh-button')
const timeline = (page: Page) => page.locator('.timeline:visible')
const bars = (page: Page) => page.getByTestId('usage-time-bar')
const labels = { '7d': '近7天', month: '本月', year: '今年' }
const counts = { '7d': 7, month: 31, year: 12 }
type Period = keyof typeof labels
const gb = (value: string) => Number(value) > 0 && Number(value) < 10_000_000 ? '< 0.01 GB' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(Number(value) / 1e9) + ' GB'

async function openUsage(page: Page, id = adminId) {
  await page.goto('/app/#/services/' + id)
  await page.getByRole('tab', { name: '流量用量', exact: true }).click()
}
async function logout(page: Page) {
  await page.locator('.shell-account-menu summary').click()
  await page.getByRole('button', { name: '退出登录', exact: true }).click()
  await expect(page.getByLabel('账号', { exact: true })).toBeVisible()
}
async function loginAfterClockJump(page: Page) {
  // 会话自然到期后允许应用返回已保存的服务详情；以已认证本人会话判定，而非强制改返回目标。
  await page.goto('/app/#/login')
  await page.getByLabel('账号', { exact: true }).fill('fixture-admin')
  await page.getByLabel('密码', { exact: true }).fill('Local-fixture-only-2026')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.locator('.shell-header')).toBeVisible()
  const session = await page.request.get('/api/v1/session')
  expect(session.status()).toBe(200)
  const data = (await session.json()).data
  expect(data.authenticated).toBe(true)
  expect(data.user.username).toBe('fixture-admin')
}
async function readUsage(page: Page, id = adminId, period: Period = '7d') {
  const response = await page.request.get(usagePath(id, period))
  expect(response.status()).toBe(200)
  return (await response.json()).data
}
// 挂起真实隔离API的响应；不伪造成功用量，也不保存认证头或下载链接。
async function holdUsage(page: Page, id: string, period: Period, status?: number) {
  const pattern = '**' + usagePath(id, period)
  let release!: () => void, arrived!: () => void, settled!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  const ready = new Promise<void>(resolve => { arrived = resolve })
  const finished = new Promise<void>(resolve => { settled = resolve })
  let count = 0
  const handler = async (route: Route) => {
    count++
    const response = await route.fetch()
    arrived()
    await gate
    // 取消后的旧响应允许被浏览器丢弃；当前响应失败仍由页面断言捕获。
    await route.fulfill(status ? { status, json: { error: { code: 'fixture_unavailable', message: '用量读取暂不可用' } } } : { response }).catch(() => {})
    settled()
  }
  await page.route(pattern, handler)
  return { ready, count: () => count, release: async () => { release(); await finished; await page.unroute(pattern, handler) } }
}
async function initialLoading(page: Page) {
  await expect(page.getByTestId('usage-timeline-loading')).toBeVisible()
  await expect(bars(page)).toHaveCount(0)
  await expect(timeline(page).locator('.range-totals')).toHaveCount(0)
  await expect(timeline(page)).not.toContainText('未记录')
  await expect(timeline(page)).not.toContainText('0 GB')
}
async function loaded(page: Page, period: Period, count = counts[period]) {
  await expect(page.getByTestId('usage-timeline-loading')).toHaveCount(0)
  await expect(bars(page)).toHaveCount(count)
  await expect(page.getByRole('radio', { name: labels[period], exact: true })).toBeChecked()
  await expect(timeline(page)).toHaveAttribute('aria-busy', 'false')
}
async function freshPage(browser: Browser, viewport: { width: number; height: number } | null | undefined, username = 'fixture-user', touch = false) {
  const context = await browser.newContext({ viewport, hasTouch: touch, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', baseURL: process.env.ACCEPTANCE_BASE_URL })
  const page = await context.newPage()
  await page.clock.setFixedTime(new Date(fixture.fixed_time))
  await login(page, username)
  return { context, page }
}

test('CP12 流量加载、缓存与失效过程', async ({ page, browser }, info) => {
  test.setTimeout(240_000)
  fixtureControl('usage-rate-reset')
  await page.clock.setFixedTime(new Date(fixture.fixed_time))
  const cp = scenario('CP12', info), network = networkEvidence(page, info, 'CP12')
  const totals = new Map<Period, string>()
  try {
    await login(page)
    await cp.check('CP12-A01', async () => {
      for (const period of ['7d', 'month', 'year'] as const) {
        const held = await holdUsage(page, adminId, period)
        try {
          if (period === '7d') await openUsage(page)
          else await choose(page, labels[period])
          await held.ready
          await initialLoading(page)
          await cp.shot(page, 'usage-loading-' + period, { allowLoading: true })
          await initialLoading(page)
        } finally { await held.release() }
        await loaded(page, period)
        const actual = await readUsage(page, adminId, period)
        await expect(timeline(page).locator('.range-totals')).toContainText(gb(actual.timeseries.totals.charged_bytes))
        totals.set(period, await timeline(page).locator('.range-totals').innerText())
      }
    })
    await cp.check('CP12-A02', async () => {
      await choose(page, '原始上传')
      const held = await holdUsage(page, adminId, 'month')
      try {
        await choose(page, '本月'); await held.ready
        await expect(bars(page)).toHaveCount(31)
        await expect(timeline(page).locator('.range-totals')).toHaveText(totals.get('month')!, { useInnerText: true })
        await expect(page.getByRole('radio', { name: '原始上传', exact: true })).toBeChecked()
        await expect(timeline(page)).toContainText('统计截至 2026/10/18 12:00')
        await expect(page.getByTestId('usage-timeline-loading')).toHaveCount(0)
        await expect(refresh(page)).toHaveAttribute('aria-busy', 'true')
        await cp.shot(page, 'usage-cache-pending', { allowLoading: true })
      } finally { await held.release() }
      await loaded(page, 'month')
    })
    await cp.check('CP12-A03', async () => {
      await page.getByRole('link', { name: '← 我的服务', exact: true }).click()
      await expect(page.locator('.service-card')).toHaveCount(1)
      const held = await holdUsage(page, adminId, 'month')
      try {
        await page.getByRole('link', { name: /查看服务与使用方法/ }).click()
        await page.getByRole('tab', { name: '流量用量', exact: true }).click(); await held.ready
        await expect(page.getByRole('radio', { name: '本月', exact: true })).toBeChecked()
        await expect(bars(page)).toHaveCount(31)
        await expect(timeline(page).locator('.range-totals')).toHaveText(totals.get('month')!, { useInnerText: true })
        await expect(page.getByTestId('usage-timeline-loading')).toHaveCount(0)
      } finally { await held.release() }
      // 新读路径不产生任何永久流量/会话副本；只检查存储键和值，不输出秘密。
      expect(await page.evaluate(() => [...Object.entries(localStorage), ...Object.entries(sessionStorage)].some(([key, value]) => /usage|timeseries|charged_bytes|csrf|cookie|token/i.test(key + value)))).toBe(false)
    })
    await cp.check('CP12-A04', async () => {
      const before = await timeline(page).innerText()
      const held = await holdUsage(page, adminId, 'month', 503)
      await refresh(page).click(); await held.ready; await held.release()
      await expect(refresh(page)).toHaveAttribute('aria-busy', 'false')
      await expect(timeline(page)).toHaveText(before, { useInnerText: true })
      await expect(page.locator('.refresh-feedback')).toContainText('读取失败')
      await expect(page.locator('main .el-alert:visible')).toHaveCount(0)
      await cp.shot(page, 'usage-cache-failed')
      await refresh(page).click(); await loaded(page, 'month')
      await expect(page.locator('.refresh-feedback')).not.toContainText('读取失败')
      const isolated = await freshPage(browser, info.project.use.viewport)
      try {
        const failed = await holdUsage(isolated.page, userId, '7d', 503)
        await openUsage(isolated.page, userId); await failed.ready; await failed.release()
        await expect(refresh(isolated.page)).toHaveAttribute('aria-busy', 'false')
        await expect(bars(isolated.page)).toHaveCount(0)
        await expect(isolated.page.getByTestId('usage-unavailable')).toBeVisible()
        await expect(isolated.page.getByTestId('usage-overview').filter({ visible: true })).not.toContainText('未记录')
        await expect(isolated.page.getByTestId('usage-overview').filter({ visible: true })).not.toContainText('0 GB')
        await expect(isolated.page.getByRole('button', { name: '重试读取数据', exact: true })).toBeVisible()
        await refresh(isolated.page).click(); await loaded(isolated.page, '7d')
      } finally { await isolated.context.close() }
    })
    await cp.check('CP12-A05', async () => {
      for (const status of [undefined, 503]) {
        const held = await holdUsage(page, adminId, '7d', status)
        await choose(page, '近7天'); await held.ready
        await choose(page, '今年'); await loaded(page, 'year')
        const current = await timeline(page).innerText()
        await held.release()
        await expect(timeline(page)).toHaveText(current, { useInnerText: true })
        await expect(page.locator('.refresh-feedback')).not.toContainText('读取失败')
      }
      // 当前每账号仅一份权益；不同服务目标使用真实不可访问对象，不能伪造多服务能力。
      for (const status of [undefined, 503]) {
        const held = await holdUsage(page, adminId, 'month', status)
        await choose(page, '本月'); await held.ready
        await page.goto('/app/#/services/' + userId)
        await expect(page.locator('main .el-alert')).toBeVisible()
        const denied = await page.locator('main .el-alert').innerText()
        await held.release()
        expect(new URL(page.url()).hash).toBe('#/services/' + userId)
        await expect(page.locator('main .el-alert')).toHaveText(denied, { useInnerText: true })
        await expect(bars(page)).toHaveCount(0)
        await expect(page.locator('[data-chart="quota-gauge"]')).toHaveCount(0)
        await expect(page.getByRole('tab', { name: '连接设置', exact: true })).toHaveCount(0)
        await openUsage(page); await loaded(page, 'month'); await choose(page, '今年'); await loaded(page, 'year')
      }
      const hanging = await holdUsage(page, adminId, 'year')
      await refresh(page).click(); await hanging.ready
      await refresh(page).evaluate((button: HTMLButtonElement) => { button.click(); button.click() })
      expect(hanging.count()).toBe(1)
      await expect(refresh(page)).toHaveAttribute('aria-busy', 'false', { timeout: 20_000 })
      await expect(page.locator('.refresh-feedback')).toContainText('读取失败')
      await expect(bars(page)).toHaveCount(12)
      await hanging.release()
      await refresh(page).click(); await loaded(page, 'year')
    })
    await cp.check('CP12-A06', async () => {
      const old = await holdUsage(page, adminId, 'month')
      await choose(page, '本月'); await old.ready
      await logout(page); await login(page, 'fixture-user'); await openUsage(page, userId)
      await loaded(page, '7d'); await old.release()
      await expect(page.locator('.page-title')).not.toContainText(adminId)
      await expect(timeline(page).locator('.range-totals')).toContainText('0 GB')
      await expect(timeline(page).locator('.range-totals')).not.toHaveText(totals.get('month')!, { useInnerText: true })
      await logout(page); await login(page)
      const cleared = await holdUsage(page, adminId, '7d')
      await openUsage(page); await cleared.ready; await initialLoading(page); await cleared.release(); await loaded(page, '7d')
      for (const status of [403, 404, 401]) {
        const pattern = '**' + usagePath(adminId, '7d')
        await page.route(pattern, route => route.fulfill({ status, json: { error: { code: 'fixture_denied', message: '此用量读取已撤销' } } }))
        await refresh(page).click()
        await expect(bars(page)).toHaveCount(0)
        await expect(page.locator('[data-chart="quota-gauge"]')).toHaveCount(0)
        await expect(page.getByRole('tab', { name: '连接设置', exact: true })).toHaveCount(0)
        await page.unroute(pattern)
        if (status === 401) {
          // 人工401未使真实夹具会话过期；沿正常导航重新核验身份，在同页内存中继续验缓存已清。
          await page.goto('/app/#/services')
          await expect(page.getByRole('heading', { name: '我的服务', exact: true })).toBeVisible()
        }
        else { await page.getByRole('link', { name: '← 我的服务', exact: true }).click(); await expect(page.locator('.service-card')).toHaveCount(1) }
        const retry = await holdUsage(page, adminId, '7d')
        await openUsage(page); await retry.ready; await initialLoading(page); await retry.release(); await loaded(page, '7d')
      }
    })
    await cp.check('CP12-A07', async () => {
      // 页面持续挂载；旧请求于日界后10ms返回，早于日界+25ms定时器，不能回填旧期。
      cp.result.boundary_events = []
      for (const [before, after, period] of [
        ['2026-10-18T15:59:59+00:00', '2026-10-18T16:00:00.010+00:00', '7d'],
        ['2026-10-19T15:59:59+00:00', '2026-10-19T16:00:00.010+00:00', 'month'],
      ] as const) {
        fixtureControl('usage-clock', before)
        const context = await browser.newContext({ viewport: info.project.use.viewport, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', baseURL: process.env.ACCEPTANCE_BASE_URL })
        try {
          const mounted = await context.newPage()
          await mounted.clock.install({ time: new Date(Date.parse(before) - 2000) })
          await mounted.clock.pauseAt(new Date(before))
          await login(mounted); await openUsage(mounted); await choose(mounted, labels[period]); await loaded(mounted, period)
          const url = mounted.url()
          const old = await holdUsage(mounted, adminId, period)
          await refresh(mounted).click(); await old.ready
          fixtureControl('usage-clock', after)
          await mounted.clock.runFor(1010)
          expect(await mounted.evaluate(() => Date.now())).toBe(Date.parse(after))
          await old.release()
          // 这里不能让时钟继续跑到日界定时器后再断言，必须拒绝刚迟到的旧结果。
          await expect(bars(mounted)).toHaveCount(0)
          await expect(mounted.locator('[data-chart="quota-gauge"]')).toHaveCount(0)
          expect(mounted.url()).toBe(url)
          const fresh = await holdUsage(mounted, adminId, period)
          await mounted.clock.runFor(1015); await fresh.ready
          await initialLoading(mounted); await fresh.release(); await loaded(mounted, period)
          const current = await readUsage(mounted, adminId, period)
          expect(current.timeseries.buckets.map((bucket: any) => bucket.start)).toEqual(await bars(mounted).evaluateAll(nodes => nodes.map(node => node.getAttribute('data-start'))))
          cp.result.boundary_events.push({ boundary: period === '7d' ? 'natural-day' : 'billing-reset', before, old_response_released_at: after, navigated: false, old_graph_removed: true, new_range_revalidated: true })
        } finally { await context.close() }
      }
      // 边界前后相隔两秒，小于TTL；只有自然边界失效能通过此反例。
      cp.result.calendar_events = []
      for (const [before, after, period] of [
        ['2026-10-18T15:59:59+00:00', '2026-10-18T16:00:01+00:00', '7d'],
        ['2026-10-19T15:59:59+00:00', '2026-10-19T16:00:01+00:00', 'month'],
        ['2026-10-31T15:59:59+00:00', '2026-10-31T16:00:01+00:00', 'month'],
        ['2026-12-31T15:59:59+00:00', '2026-12-31T16:00:01+00:00', 'year'],
      ] as const) {
        fixtureControl('usage-clock', before); await page.clock.setFixedTime(new Date(before))
        // 大幅推进日期会让一小时夹具会话自然到期；先重新登录，再验相隔两秒的同身份缓存。
        await page.reload(); await loginAfterClockJump(page); await openUsage(page); await choose(page, labels[period])
        await loaded(page, period, period === 'month' && before.startsWith('2026-10-19') ? 31 : counts[period])
        await page.getByRole('link', { name: '← 我的服务', exact: true }).click()
        fixtureControl('usage-clock', after); await page.clock.setFixedTime(new Date(after))
        const held = await holdUsage(page, adminId, period)
        await page.getByRole('link', { name: /查看服务与使用方法/ }).click()
        await page.getByRole('tab', { name: '流量用量', exact: true }).click(); await held.ready
        await initialLoading(page); expect(held.count()).toBe(1)
        await held.release()
        await loaded(page, period, period === 'month' && after.startsWith('2026-10-31') ? 30 : counts[period])
        const current = await readUsage(page, adminId, period)
        expect(Date.parse(current.generated_at)).toBe(Date.parse(after))
        expect(current.timeseries.buckets.map((bucket: any) => bucket.start)).toEqual(await bars(page).evaluateAll(nodes => nodes.map(node => node.getAttribute('data-start'))))
        cp.result.calendar_events.push({ before, after, period, cache_expired: true, actual_bucket_count: await bars(page).count() })
      }
      await cp.shot(page, 'boundary-revalidated')
      fixtureControl('usage-clock', fixture.fixed_time); await page.clock.setFixedTime(new Date(fixture.fixed_time))
      await page.reload(); await openUsage(page); await loaded(page, '7d')
      await page.getByRole('link', { name: '← 我的服务', exact: true }).click()
      await page.clock.setFixedTime(new Date(Date.parse(fixture.fixed_time) + 5 * 60_000 + 1))
      const expired = await holdUsage(page, adminId, '7d')
      await openUsage(page); await expired.ready; await initialLoading(page); await expired.release()
    })
    cp.finish()
  } finally {
    fixtureControl('usage-clock', fixture.fixed_time)
    cp.result.network_log = network(); save(info)
  }
})

test('CP13 柱图真实悬浮、键盘和触屏交互', async ({ page, browser }, info) => {
  fixtureControl('usage-rate-reset')
  await page.clock.setFixedTime(new Date(fixture.fixed_time))
  const cp = scenario('CP13', info)
  try {
    await login(page); await openUsage(page); await choose(page, '本月'); await loaded(page, 'month')
    const data = await readUsage(page, adminId, 'month')
    const recorded = data.timeseries.buckets.find((bucket: any) => bucket.charged_bytes !== null && BigInt(bucket.charged_bytes) > 0n)
    const target = bars(page).filter({ has: page.locator('.actual-bar') }).filter({ hasNot: page.locator('.actual-bar.zero') }).first()
    const card = page.getByTestId('usage-point-card')
    const alignChart = async (targetPage: Page) => targetPage.locator('.history-chart').evaluate(element => window.scrollTo(0, window.scrollY + element.getBoundingClientRect().top - 80))
    async function cardValue(metric: 'charged_bytes' | 'upload_bytes' | 'download_bytes', name: string) {
      await expect(page.getByTestId('usage-point-date')).toHaveText(recorded.start.slice(0, 10))
      await expect(page.getByTestId('usage-point-metric')).toHaveText(name)
      await expect(page.getByTestId('usage-point-value')).toHaveText(gb(recorded[metric]))
      await expect(page.getByTestId('usage-point-exact')).toHaveText(BigInt(recorded[metric]).toLocaleString('zh-CN') + ' 字节')
    }
    await cp.check('CP13-A01', async () => {
      await alignChart(page)
      await target.hover(); await expect(card).toBeVisible(); await cardValue('charged_bytes', '套餐消耗')
      await expect(target).toHaveClass(/selected/); await expect(target).not.toHaveAttribute('title')
      const style = await card.evaluate(element => { const s = getComputedStyle(element); return { border: parseFloat(s.borderTopWidth), padding: parseFloat(s.paddingTop), background: s.backgroundColor } })
      expect(style.border).toBeGreaterThan(0); expect(style.padding).toBeGreaterThan(0); expect(style.background).not.toBe('rgba(0, 0, 0, 0)')
      expect(await page.getByTestId('usage-point-value').evaluate(element => parseFloat(getComputedStyle(element).fontSize))).toBeGreaterThan(await page.getByTestId('usage-point-metric').evaluate(element => parseFloat(getComputedStyle(element).fontSize)))
      await expect(card).toBeInViewport({ ratio: 1 })
      await cp.shot(page, 'usage-card-hover', { preservePointer: true })
    })
    await cp.check('CP13-A02', async () => {
      await page.mouse.move(0, 0); await target.focus(); await cardValue('charged_bytes', '套餐消耗')
      await expect(target).toBeFocused(); await expect(target).toHaveAccessibleName(new RegExp(recorded.start.slice(0, 10)))
      await expect(card).toBeInViewport({ ratio: 1 })
      await cp.shot(page, 'usage-card-focus', { preservePointer: true })
      await page.keyboard.press('Escape'); await expect(card).toHaveCount(0)
      await page.keyboard.press('Tab'); await expect(card).toBeVisible()
    })
    await cp.check('CP13-A03', async () => {
      await target.click(); await page.mouse.move(0, 0)
      await expect(card).toHaveAttribute('data-pinned', 'true'); await expect(target).toHaveAttribute('aria-pressed', 'true')
      await target.click(); await expect(card).toHaveCount(0)
      const touch = await freshPage(browser, info.project.use.viewport, 'fixture-admin', true)
      try {
        await openUsage(touch.page); await loaded(touch.page, '7d')
        await alignChart(touch.page)
        const first = bars(touch.page).first(), last = bars(touch.page).last()
        await first.tap(); await expect(touch.page.getByTestId('usage-point-card')).toHaveAttribute('data-pinned', 'true')
        await expect(first).toHaveAttribute('aria-pressed', 'true')
        await last.tap(); await expect(last).toHaveAttribute('aria-pressed', 'true'); await expect(first).toHaveAttribute('aria-pressed', 'false')
        await expect(touch.page.getByTestId('usage-point-date')).toHaveText((await last.getAttribute('data-start'))!.slice(0, 10))
        await expect(touch.page.getByTestId('usage-point-card')).toBeInViewport({ ratio: 1 })
        await cp.shot(touch.page, 'usage-card-touch', { preservePointer: true })
        await last.tap(); await expect(touch.page.getByTestId('usage-point-card')).toHaveCount(0)
        await first.tap(); await choose(touch.page, '本月'); await loaded(touch.page, 'month')
        await expect(touch.page.getByTestId('usage-point-card')).toHaveCount(0)
        await noOverflow(touch.page)
      } finally { await touch.context.close() }
    })
    await cp.check('CP13-A04', async () => {
      await target.click()
      for (const [metric, name] of [['charged_bytes', '套餐消耗'], ['upload_bytes', '原始上传'], ['download_bytes', '原始下载']] as const) {
        await choose(page, name); await cardValue(metric, name)
      }
      for (const [selector, expected] of [['.actual-bar.zero', '0 GB'], ['.future-bar', '未到该时段']] as const) {
        const point = bars(page).filter({ has: page.locator(selector) }).first()
        await point.click(); await expect(page.getByTestId('usage-point-value')).toHaveText(expected)
        if (expected !== '0 GB') await expect(page.getByTestId('usage-point-exact')).toHaveCount(0)
      }
      await choose(page, '今年'); await loaded(page, 'year')
      await bars(page).filter({ has: page.locator('.missing-bar') }).first().click()
      await expect(page.getByTestId('usage-point-value')).toHaveText('未记录')
      await expect(page.getByTestId('usage-point-exact')).toHaveCount(0)
      await choose(page, '本月'); await loaded(page, 'month')
    })
    await cp.check('CP13-A05', async () => {
      for (const index of [0, 15, 30]) {
        const point = bars(page).nth(index)
        await point.click(); await expect(point).toHaveClass(/selected/)
        await card.scrollIntoViewIfNeeded(); await expect(card).toBeInViewport({ ratio: 1 })
        const box = await card.boundingBox(); expect(box!.x).toBeGreaterThanOrEqual(0); expect(box!.x + box!.width).toBeLessThanOrEqual(info.project.use.viewport!.width)
        await noOverflow(page)
      }
      await page.getByRole('button', { name: '查看线路使用明细', exact: true }).click()
      await expect(page.locator('.el-drawer')).toContainText('HOME 线路'); await expect(page.locator('.el-drawer')).toContainText('BWH 线路')
      await page.locator('.el-drawer__close-btn').click()
      for (const period of ['7d', 'year', 'month'] as const) { await choose(page, labels[period]); await loaded(page, period) }
    })
    cp.finish()
  } finally { save(info) }
})

test('CP14 服务状态、资料与管理员计量诊断', async ({ page, browser }, info) => {
  fixtureControl('usage-rate-reset')
  await page.clock.setFixedTime(new Date(fixture.fixed_time))
  const cp = scenario('CP14', info), network = networkEvidence(page, info, 'CP14')
  const diagnosticPath = `/api/v1/admin/services/${adminId}/metering?period=7d`
  const readService = async () => (await (await page.request.get('/api/v1/me/services/' + adminId)).json()).data
  let original: any, originalUsage: any
  try {
    await login(page)
    original = await readService(); originalUsage = await readUsage(page)
    await cp.check('CP14-A01', async () => {
      for (const [state, expectedState, label] of [
        ['applied', 'active', '已应用'], ['waiting', 'pending', '等待应用'],
        ['disabled', 'disabled', '已停用'], ['expired', 'expired', '已到期'],
      ] as const) {
        fixtureControl('usage-service-state', state)
        await page.goto('/app/#/services'); await refresh(page).click()
        const detail = await readService()
        const listed = (await (await page.request.get('/api/v1/me/services')).json()).data.items.find((row: any) => row.id === adminId)
        const managed = (await (await page.request.get('/api/v1/admin/services')).json()).data.items.find((row: any) => row.id === adminId)
        expect(detail.service_status).toMatchObject({ state: expectedState, label })
        expect(listed.service_status).toEqual(detail.service_status); expect(managed.service_status).toEqual(detail.service_status)
        await expect(page.locator('.service-card .el-tag')).toHaveText(label)
        await page.getByRole('link', { name: /查看服务与使用方法/ }).click()
        await expect(page.locator('.detail-summary .metrics-grid > div').filter({ has: page.locator('dt', { hasText: /^服务状态$/ }) })).toContainText(label)
        await expect(page.locator('.detail-summary')).not.toContainText('用量待核算')
        await expect(page.locator('.detail-summary')).not.toContainText('线路在线')
      }
      fixtureControl('usage-service-state', 'applied')
      await page.goto('/app/#/services'); await refresh(page).click()
      await expect(page.locator('.service-card .el-tag')).toHaveText('已应用')
      await cp.shot(page, 'service-business-state')
    })
    await cp.check('CP14-A04', async () => {
      const listed = (await (await page.request.get('/api/v1/me/services')).json()).data.items.find((row: any) => row.id === adminId)
      const detail = await readService()
      expect(listed.name).toBe(detail.name)
      await expect(page.locator('.service-card h2')).toHaveText(listed.name)
      await expect(page.locator('.service-card h2')).not.toContainText(adminId.slice(0, 8))
      await page.locator('.service-information summary').click()
      await expect(page.locator('.service-information')).toContainText('服务编号')
      await expect(page.locator('.service-information')).toContainText(adminId)
      await expect(page.getByRole('link', { name: /查看服务与使用方法/ })).toHaveAttribute('href', '#/services/' + adminId)
      await page.getByRole('link', { name: /查看服务与使用方法/ }).click()
      await expect(page.locator('.page-title h1')).toHaveText(detail.name)
      await expect(page.locator('.detail-summary .service-number')).toContainText(adminId)
      expect(new URL(page.url()).hash).toBe('#/services/' + adminId)
      await cp.shot(page, 'service-identifiers')
      await noOverflow(page)
    })
    await cp.check('CP14-A02', async () => {
      for (const label of ['服务概览', '流量用量']) {
        await page.getByRole('tab', { name: label, exact: true }).click()
        if (label === '流量用量') await loaded(page, '7d')
        for (const diagnostic of ['采样覆盖', '未分桶', '来源指纹', '历史衔接', '无损尾部计量', '硬限额执行']) {
          await expect(page.locator('main')).not.toContainText(diagnostic)
        }
        await expect(page.locator('.metering-diagnostics')).toHaveCount(0)
      }
      const ordinary = await freshPage(browser, info.project.use.viewport)
      try {
        expect((await ordinary.page.request.get(diagnosticPath)).status()).toBe(403)
        const own = await readUsage(ordinary.page, userId)
        for (const key of ['metering', 'metering_diagnostics', 'historical_baseline', 'limitations', 'credentials', 'private_key']) expect(own).not.toHaveProperty(key)
        await openUsage(ordinary.page, userId); await loaded(ordinary.page, '7d')
        await expect(ordinary.page.getByRole('button', { name: '计量详情', exact: true })).toHaveCount(0)
        await expect(ordinary.page.locator('.metering-diagnostics')).toHaveCount(0)
      } finally { await ordinary.context.close() }
    })
    await cp.check('CP14-A03', async () => {
      const response = await page.request.get(diagnosticPath)
      expect(response.status()).toBe(200)
      const diagnostics = (await response.json()).data
      expect(Object.keys(diagnostics).sort()).toEqual(['service_id', 'period', 'generated_at', 'service_status', 'metering', 'historical_baseline', 'coverage', 'limitations'].sort())
      expect(Object.keys(diagnostics.metering).sort()).toEqual(['quality', 'evidence_quality', 'updated_at', 'persisted_gap', 'active_identity_count', 'evidence_identity_count', 'unverified_identity_count', 'ambiguous_identity_count', 'partial_observed_identity_count', 'reason_codes'].sort())
      expect(diagnostics.service_id).toBe(adminId)
      expect(diagnostics.metering.quality).toBe((await readService()).usage.quality)
      expect(diagnostics.coverage.range_start).toBe(originalUsage.timeseries.range_start)
      expect(diagnostics.coverage.unallocated_charged_bytes).toBe(originalUsage.timeseries.totals.unallocated_charged_bytes)
      expect(diagnostics.limitations).toEqual({ lossless_tail_verified: false, hard_limit_verified: false, revocation_verified: false })
      for (const key of ['credentials', 'private_key', 'source_config_fingerprint', 'identity_ids', 'raw_config']) expect(JSON.stringify(diagnostics)).not.toContain('"' + key + '"')
      await adminNavigation(page, '管理面板')
      await adminNavigation(page, '订阅管理')
      const row = page.locator('.el-table__row').filter({ hasText: 'fixture-admin' })
      await row.getByRole('button', { name: '计量详情', exact: true }).click()
      const panel = page.locator('.metering-diagnostics')
      await expect(panel).toContainText(adminId)
      for (const label of ['服务登记状态', '计量质量', '采样证据', '实际采样时刻', '范围覆盖', '原因与核对方向', '能力验收边界', '尚未验证']) await expect(panel).toContainText(label)
      await expect(panel).not.toContainText(userId)
      await cp.shot(page, 'admin-metering-diagnostics')
      await noOverflow(page)
      await page.locator('.el-drawer__close-btn:visible').click()
      // 管理筛选仍使用原计量维度，不能因新服务主状态而把所有服务抬成active。
      await page.locator('.table-filters .el-select').click()
      await page.getByRole('option', { name: '计量存在缺口', exact: true }).click()
      const filtered = (await (await page.request.get('/api/v1/admin/services?state=metering_gap')).json()).data.items
      await expect(page.locator('.el-table__row')).toHaveCount(filtered.length)
      if (filtered.length) expect(await page.locator('.el-table__row').allTextContents()).toEqual(expect.arrayContaining(filtered.map((item: any) => expect.stringContaining(item.user.username))))
      await page.locator('.table-filters .el-select').click()
      await page.getByRole('option', { name: '全部状态', exact: true }).click()
      await expect(page.locator('.el-table__row')).not.toHaveCount(0)
    })
    await cp.check('CP14-A05', async () => {
      fixtureControl('usage-service-state', 'restore')
      const restored = await readService(), restoredUsage = await readUsage(page)
      for (const key of ['id', 'quota_bytes', 'used_bytes', 'remaining_bytes', 'recorded_used_bytes', 'recorded_upload_bytes', 'recorded_download_bytes', 'expires_at', 'next_reset_at', 'usage', 'usage_estimate']) expect(restored[key]).toEqual(original[key])
      expect(restoredUsage.summary).toEqual(originalUsage.summary)
      expect(restoredUsage.quality).toEqual(originalUsage.quality)
      expect(restoredUsage.timeseries).toEqual(originalUsage.timeseries)
      await page.goto('/app/#/services/' + adminId)
      await expect(page.locator('[data-chart="quota-gauge"]')).toBeVisible()
      await expect(page.locator('[data-chart="transfer-composition"]')).toBeVisible()
      await expect(page.locator('.quota-legend')).toContainText(fixture.expected.charged_gb)
      await expect(page.locator('.quota-legend')).toContainText(fixture.expected.remaining_gb)
      await page.getByRole('tab', { name: '流量用量', exact: true }).click()
      for (const period of ['7d', 'month', 'year'] as const) { await choose(page, labels[period]); await loaded(page, period) }
    })
    cp.finish()
  } finally {
    fixtureControl('usage-service-state', 'restore')
    cp.result.network_log = network(); save(info)
  }
})
