import { expect, type BrowserContext, type Page } from '@playwright/test'
import { test, scenario, save, fixture, login, adminNavigation, noOverflow, fixtureControl } from './helpers'

const serviceId = fixture.service_ids['fixture-unknown']
const oldTime = '2026-10-18T04:00:00+00:00'
const newTime = '2026-10-21T04:00:00+00:00'
async function data(page: Page, path: string) {
  const response = await page.request.get('/api/v1/' + path)
  expect(response.status()).toBe(200)
  return (await response.json()).data
}
async function plainOverview(page: Page, used: string, remaining: string) {
  await expect(page.locator('[data-chart="quota-gauge"]')).toBeVisible()
  await expect(page.locator('[data-chart="quota-gauge"]')).not.toHaveClass(/unknown/)
  await expect(page.locator('[data-chart="transfer-composition"]')).toBeVisible()
  const legend = page.locator('.quota-legend')
  await expect(legend).toContainText('套餐已用')
  await expect(legend).toContainText(used)
  await expect(legend).toContainText('剩余流量')
  await expect(legend).toContainText(remaining)
  for (const text of ['待核算', '历史衔接', '采样覆盖', '未分桶', '来源指纹', '估算']) {
    await expect(page.locator('main')).not.toContainText(text)
  }
  await expect(page.locator('.metering-diagnostics')).toHaveCount(0)
  await noOverflow(page)
}

test('CP15 换期后套餐余额沿用本期已记录账本', async ({ browser }, info) => {
  const cp = scenario('CP15', info)
  const contexts: BrowserContext[] = []
  const fresh = async (username: string, time: string) => {
    // 跨自然日必须真实重新认证；不延长或绕过产品会话有效期。
    const context = await browser.newContext({ baseURL: process.env.ACCEPTANCE_BASE_URL,
      viewport: info.project.use.viewport, locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
      colorScheme: 'light', reducedMotion: 'reduce' })
    contexts.push(context)
    const page = await context.newPage()
    await page.clock.setFixedTime(new Date(time))
    await login(page, username)
    return page
  }
  fixtureControl('usage-rate-reset')
  let original: any
  try {
    const before = await fresh('fixture-unknown', fixture.fixed_time)
    original = await data(before, 'me/services/' + serviceId)
    await before.context().close()
    fixtureControl('balance-profile', 'old')
    const old = await fresh('fixture-unknown', oldTime)
    await cp.check('CP15-A01', async () => {
      const detail = await data(old, 'me/services/' + serviceId)
      expect(detail.usage_estimate).toMatchObject({ used_bytes: '98253546067', remaining_bytes: '901746453933' })
      await expect(old.locator('.service-card')).toContainText('套餐已用 98.25 GB')
      await expect(old.locator('.card-facts')).toContainText('901.74 GB')
      await old.getByRole('link', { name: /查看服务与使用方法/ }).click()
      await plainOverview(old, '98.25 GB', '901.74 GB')
      await cp.shot(old, 'balance-old-cycle')
    })
    await old.context().close()
    expect(fixtureControl('balance-profile', 'new').ledger_quality).toBe('partial_observed')
    const page = await fresh('fixture-unknown', newTime)
    const admin = await fresh('fixture-admin', newTime)
    await cp.check('CP15-A02', async () => {
      const listed = (await data(page, 'me/services')).items.find((row: any) => row.id === serviceId)
      const detail = await data(page, 'me/services/' + serviceId)
      const managed = (await data(admin, 'admin/services')).items.find((row: any) => row.id === serviceId)
      for (const row of [listed, detail, managed]) {
        expect(row).toMatchObject({ id: serviceId, quota_bytes: '1000000000000',
          used_bytes: null, remaining_bytes: null, recorded_used_bytes: '5690000000',
          recorded_remaining_bytes: '994310000000', recorded_upload_bytes: '1240000000',
          recorded_download_bytes: '4450000000', usage_estimate: null })
        expect(row.usage.quality).toBe('gap')
      }
      const usage = await data(page, 'me/services/' + serviceId + '/usage?period=7d')
      expect(usage.summary).toMatchObject({ charged_bytes: null, remaining_bytes: null,
        recorded_used_bytes: '5690000000', recorded_remaining_bytes: '994310000000', usage_estimate: null })
      expect(Date.parse(usage.current_cycle.starts_at)).toBe(Date.parse('2026-10-19T16:00:00Z'))
      expect(Date.parse(usage.current_cycle.ends_at)).toBe(Date.parse('2026-11-19T16:00:00Z'))
    })
    await cp.check('CP15-A03', async () => {
      await expect(page.locator('.service-card')).toContainText('套餐已用 5.69 GB')
      await expect(page.locator('.card-facts')).toContainText('994.31 GB')
      await expect(page.locator('.service-card')).not.toContainText('待核算')
      await noOverflow(page)
      await cp.shot(page, 'balance-new-cycle-list')
      await page.getByRole('link', { name: /查看服务与使用方法/ }).click()
      await plainOverview(page, '5.69 GB', '994.31 GB')
      await expect(page.locator('.transfer-legend')).toContainText('1.24 GB')
      await expect(page.locator('.transfer-legend')).toContainText('4.45 GB')
      await cp.shot(page, 'balance-new-cycle-detail')
      await adminNavigation(admin, '管理面板')
      await adminNavigation(admin, '订阅管理')
      const row = admin.locator('.el-table__row').filter({ hasText: 'fixture-unknown' })
      await expect(row).toContainText('5.69 GB')
      await expect(row).toContainText('994.31 GB')
      await expect(row).not.toContainText('待核算')
      await expect(row).not.toContainText('已记录用量')
      // 窄屏表格可横向滚动；图证必须实际看见余额列，不能仅断言隐藏文本。
      await row.getByText('994.31 GB', { exact: true }).scrollIntoViewIfNeeded()
      await expect(row.getByText('994.31 GB', { exact: true })).toBeInViewport()
      await noOverflow(admin)
      await cp.shot(admin, 'balance-admin')
    })
    await cp.check('CP15-A04', async () => {
      const diagnostics = await data(admin, 'admin/services/' + serviceId + '/metering?period=7d')
      // 原完整证据质量仍为unknown；部分观测另由计数解释，不能提升原门禁。
      expect(diagnostics.metering).toMatchObject({ quality: 'gap', evidence_quality: 'unknown',
        partial_observed_identity_count: 1, evidence_identity_count: 0 })
      expect(diagnostics.historical_baseline.state).not.toBe('applied')
      expect(diagnostics.limitations).toEqual({ lossless_tail_verified: false, hard_limit_verified: false, revocation_verified: false })
      await admin.locator('.el-table__row').filter({ hasText: 'fixture-unknown' })
        .getByRole('button', { name: '计量详情', exact: true }).click()
      await expect(admin.locator('.metering-diagnostics')).toContainText('部分观测身份1')
      await expect(admin.locator('.metering-diagnostics')).toContainText('已记录部分采样金额，尚不能确认完整区间覆盖')
      await expect(admin.locator('.metering-diagnostics')).toContainText('当前账期没有一次性历史衔接量')
      await noOverflow(admin)
      await page.context().close()
      await admin.context().close()
      expect(fixtureControl('balance-profile', 'restore')).toMatchObject({ restored: true,
        new_records_removed: true, original_service_preserved: true })
      const restoredPage = await fresh('fixture-unknown', fixture.fixed_time)
      const restored = await data(restoredPage, 'me/services/' + serviceId)
      for (const key of ['id', 'quota_bytes', 'used_bytes', 'remaining_bytes', 'recorded_used_bytes',
        'recorded_remaining_bytes', 'recorded_upload_bytes', 'recorded_download_bytes',
        'expires_at', 'next_reset_at', 'usage', 'usage_estimate']) expect(restored[key]).toEqual(original[key])
    })
    cp.finish()
  } finally {
    for (const context of contexts) await context.close()
    try { fixtureControl('balance-profile', 'restore') } finally { save(info) }
  }
})
