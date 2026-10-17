import { expect, type Page } from '@playwright/test'
import { writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { test, scenario, save, fixture, fixtureControl, login, run, hash } from './helpers'

// 前发行使用真实编译资产的确定性改名集合；报告明确记录合成来源，不冒充生产历史。
test('CP11 @desktop 旧发行兼容、持续失败、会话与未保存输入', async ({ page, browser }, info) => {
  const cp = scenario('CP11', info)
  const rows: any[] = [], transitions: any[] = []
  const metadata = fixture.releases
  let documentLoads = 0, loginPosts = 0, businessPosts = 0
  const observe = (target: Page) => {
    target.on('request', request => {
      const path = new URL(request.url()).pathname
      if (request.isNavigationRequest() && request.resourceType() === 'document') documentLoads++
      if (path === '/api/v1/login' && request.method() === 'POST') loginPosts++
      if (request.method() === 'POST' && /(?:rules|resource-checks)/.test(path)) businessPosts++
    })
    target.on('response', response => {
      const path = new URL(response.url()).pathname
      if (path.startsWith('/app/') || path.startsWith('/api/')) rows.push({ path, method: response.request().method(), status: response.status() })
    })
    target.on('requestfailed', request => rows.push({ path: new URL(request.url()).pathname,
      method: request.method(), failure: request.failure()?.errorText }))
  }
  const marker = (target: Page) => target.locator('meta[name="frontend-release"]').getAttribute('content')
  const switchRelease = (operation: string) => {
    const result = fixtureControl(operation)
    transitions.push({ operation, result })
    return result
  }
  const sessionCookie = async (target: Page) => (await target.context().cookies()).find(row => row.name === 'megabox_session')?.value
  observe(page)
  await page.clock.setFixedTime(new Date(fixture.fixed_time))
  let extraContext: Awaited<ReturnType<typeof browser.newContext>> | undefined
  try {
    expect(metadata.old.manifest_sha256).toMatch(/^[a-f0-9]{64}$/)
    expect(metadata.candidate.manifest_sha256).toMatch(/^[a-f0-9]{64}$/)
    expect(metadata.recovery_old.manifest_sha256).not.toBe(metadata.candidate.manifest_sha256)
    await cp.check('CP11-A01', async () => {
      switchRelease('release-old')
      await login(page)
      expect(await marker(page)).toBe(metadata.old.manifest_sha256)
      const session = await sessionCookie(page), loads = documentLoads
      switchRelease('release-candidate')
      await page.locator('.shell-account-menu summary').click()
      await page.locator('.shell-account-menu').getByRole('link', { name: '管理面板', exact: true }).click()
      await expect(page.getByText('我的套餐与流量', { exact: true })).toBeVisible()
      await page.getByRole('link', { name: '进入我的服务', exact: true }).click()
      await page.getByRole('link', { name: /查看服务与使用方法/ }).click()
      await expect(page.locator('[data-chart="quota-gauge"]')).toBeVisible()
      expect(new URL(page.url()).hash).toBe(`#/services/${fixture.service_ids['fixture-admin']}`)
      expect(await sessionCookie(page)).toBe(session)
      expect(documentLoads).toBe(loads)
      expect(rows.some(row => /AdminOverviewPage-.*\.js$/.test(row.path) && row.status === 200)).toBeTruthy()
      transitions.push({ case: 'previous-compiled-assets-window', kind: metadata.old.kind, reloads: documentLoads - loads, login_posts: loginPosts })
    })

    await cp.check('CP11-A02', async () => {
      await page.goto('/app/?cp11=same-version#/services')
      await expect(page.locator('.service-card')).toHaveCount(1)
      expect(await marker(page)).toBe(metadata.candidate.manifest_sha256)
      const loads = documentLoads, session = await sessionCookie(page), posts = loginPosts, writes = businessPosts
      await page.route('**/app/assets/GuidesPage-*.js', route => route.fulfill({ status: 404, contentType: 'text/plain', body: 'isolated missing module' }))
      await page.locator('.shell-header').getByRole('link', { name: '使用指南', exact: true }).click()
      await expect(page.locator('.release-recovery')).toContainText('未发现可用的新版本')
      await page.getByRole('button', { name: '重试打开', exact: true }).click()
      await expect(page.locator('.release-recovery')).toContainText('未发现可用的新版本')
      await expect(page.getByRole('heading', { name: '我的服务', exact: true })).toBeVisible()
      expect(documentLoads).toBe(loads)
      await page.unroute('**/app/assets/GuidesPage-*.js')
      await page.getByRole('button', { name: '重试打开', exact: true }).click()
      let missingRecovered = false
      try {
        await expect(page.getByRole('heading', { name: '你想完成哪一步？', exact: true })).toBeVisible({ timeout: 3000 })
        missingRecovered = true
      } catch { /* 保留导航重试失败事实，接着检验用户明确重建文档的兜底。 */ }
      expect(documentLoads).toBe(loads)
      if (!missingRecovered) {
        await page.getByRole('button', { name: '重新载入页面', exact: true }).click()
        await expect(page.getByRole('heading', { name: '你想完成哪一步？', exact: true })).toBeVisible()
        expect(documentLoads - loads).toBe(1)
      }
      expect(await sessionCookie(page)).toBe(session)
      expect(loginPosts).toBe(posts); expect(businessPosts).toBe(writes)
      transitions.push({ case: 'removed-404-retry', navigation_retry_recovered: missingRecovered, recovered: true, explicit_document_reloads: documentLoads - loads,
        login_posts: loginPosts - posts, business_posts: businessPosts - writes })
      await page.goto('/app/?cp11=offline#/services')
      // 先确认本次新文档的会话与入口已就绪，再注入模块加载断网。
      await expect(page.locator('.service-card')).toHaveCount(1)
      const offlineLoads = documentLoads
      await page.context().setOffline(true)
      await page.locator('.shell-account-menu summary').click()
      await page.locator('.shell-account-menu').getByRole('link', { name: '账号设置', exact: true }).click()
      await expect(page.locator('.release-recovery')).toContainText('当前内容已保留')
      await expect(page.getByRole('button', { name: '重试打开', exact: true })).toBeVisible()
      expect(rows.some(row => /AccountPage-.*\.js$/.test(row.path) && row.failure)).toBeTruthy()
      expect(documentLoads).toBe(offlineLoads)
      await page.context().setOffline(false)
      await page.getByRole('button', { name: '重试打开', exact: true }).click()
      let offlineRecovered = false
      try {
        await expect(page.getByRole('heading', { name: '账号设置', exact: true })).toBeVisible({ timeout: 3000 })
        offlineRecovered = true
      } catch { /* 同版本断网也可能缓存模块拒绝；不能把重试入口存在当成功。 */ }
      expect(documentLoads).toBe(offlineLoads)
      if (!offlineRecovered) {
        await page.getByRole('button', { name: '重新载入页面', exact: true }).click()
        await expect(page.getByRole('heading', { name: '账号设置', exact: true })).toBeVisible()
        expect(documentLoads - offlineLoads).toBe(1)
      }
      expect(await sessionCookie(page)).toBe(session)
      expect(loginPosts).toBe(posts); expect(businessPosts).toBe(writes)
      transitions.push({ case: 'online-again-retry', navigation_retry_recovered: offlineRecovered, recovered: true, explicit_document_reloads: documentLoads - offlineLoads,
        login_posts: loginPosts - posts, business_posts: businessPosts - writes })
      transitions.push({ case: 'same-version-missing-and-offline', automatic_reloads: 0 })
    })

    await cp.check('CP11-A03', async () => {
      switchRelease('release-recovery-old')
      extraContext = await browser.newContext({ viewport: info.project.use.viewport, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', baseURL: process.env.ACCEPTANCE_BASE_URL })
      const authPage = await extraContext.newPage(); observe(authPage)
      await authPage.clock.setFixedTime(new Date(fixture.fixed_time))
      await authPage.goto('/app/#/login')
      expect(await marker(authPage)).toBe(metadata.recovery_old.manifest_sha256)
      await authPage.route('**/app/assets/ServicesPage-*.js', route => route.fulfill({ status: 404, contentType: 'text/plain', body: 'isolated stale module' }))
      await authPage.getByLabel('账号', { exact: true }).fill('fixture-admin')
      await authPage.getByLabel('密码', { exact: true }).fill('Local-fixture-only-2026')
      switchRelease('release-candidate')
      const posts = loginPosts
      await authPage.getByRole('button', { name: '登录', exact: true }).click()
      await expect(authPage.getByText('已登录，但页面暂时未能打开。请点击“进入服务”重试。', { exact: true })).toBeVisible()
      await expect(authPage.getByRole('button', { name: '更新并继续', exact: true })).toBeVisible()
      const session = await sessionCookie(authPage)
      await authPage.getByRole('button', { name: '进入服务', exact: true }).click()
      await expect(authPage.getByRole('button', { name: '更新并继续', exact: true })).toBeVisible()
      expect(loginPosts - posts).toBe(1)
      await expect(authPage.getByLabel('密码', { exact: true })).toHaveValue('')
      await authPage.unroute('**/app/assets/ServicesPage-*.js')
      const loads = documentLoads
      authPage.once('dialog', dialog => dialog.accept())
      await authPage.getByRole('button', { name: '更新并继续', exact: true }).click()
      await expect(authPage.getByRole('heading', { name: '我的服务', exact: true })).toBeVisible()
      expect(await marker(authPage)).toBe(metadata.candidate.manifest_sha256)
      expect(await sessionCookie(authPage)).toBe(session)
      expect(documentLoads - loads).toBe(1); expect(loginPosts - posts).toBe(1)
      transitions.push({ case: 'authenticated-navigation', user_reload_count: documentLoads - loads, login_posts: loginPosts - posts })
      await extraContext.close(); extraContext = undefined
    })

    await cp.check('CP11-A04', async () => {
      switchRelease('release-recovery-old')
      await page.goto('/app/?cp11=unsaved#/admin/rules')
      await expect(page.getByRole('heading', { name: '代理规则', exact: true })).toBeVisible()
      await page.getByRole('button', { name: '添加规则', exact: true }).click()
      await page.getByLabel('规则内容', { exact: true }).fill('unsaved.example.invalid')
      await page.route('**/app/assets/AdminServersPage-*.js', route => route.fulfill({ status: 404, contentType: 'text/plain', body: 'isolated stale module' }))
      switchRelease('release-candidate')
      const loads = documentLoads, posts = businessPosts
      // 模拟浏览器地址栏hash导航；抽屉保持打开，不先关闭表单丢掉待保存值。
      await page.evaluate(() => { window.location.hash = '/admin/servers' })
      await expect(page.getByRole('button', { name: '更新并继续', exact: true })).toBeVisible()
      expect(documentLoads).toBe(loads)
      page.once('dialog', dialog => dialog.dismiss())
      await page.getByRole('button', { name: '更新并继续', exact: true }).click()
      await expect(page.getByLabel('规则内容', { exact: true })).toHaveValue('unsaved.example.invalid')
      expect(documentLoads).toBe(loads); expect(businessPosts).toBe(posts)
      // 恢复提示不能挡住仍在编辑的抽屉关闭入口；trial只验证可点击，不丢弃输入。
      await page.locator('.el-drawer__close-btn').click({ trial: true })
      const persisted = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }))
      expect(persisted).not.toContain('Local-fixture-only-2026')
      expect(persisted).not.toContain('unsaved.example.invalid')
      await cp.shot(page, 'release-recovery')
      await page.unroute('**/app/assets/AdminServersPage-*.js')
      page.once('dialog', dialog => dialog.accept())
      await page.getByRole('button', { name: '更新并继续', exact: true }).click()
      await expect(page.getByRole('heading', { name: '服务器', exact: true })).toBeVisible()
      expect(await marker(page)).toBe(metadata.candidate.manifest_sha256)
      expect(documentLoads - loads).toBe(1); expect(businessPosts).toBe(posts)
      expect(new URL(page.url()).hash).toBe('#/admin/servers')
      transitions.push({ case: 'unsaved-business-input', canceled_reload_count: 0, accepted_reload_count: documentLoads - loads, business_posts: businessPosts - posts })
    })

    await cp.check('CP11-A05', async () => {
      expect(rows.some(row => row.status === 404 && row.path.endsWith('.js'))).toBeTruthy()
      expect(rows.some(row => row.failure)).toBeTruthy()
      expect(await marker(page)).toBe(metadata.candidate.manifest_sha256)
      expect(transitions.filter(row => row.user_reload_count === 1 || row.accepted_reload_count === 1)).toHaveLength(2)
      cp.result.release_evidence = { ...metadata, transitions }
    })
    cp.finish()
  } finally {
    await page.context().setOffline(false)
    if (extraContext) await extraContext.close()
    switchRelease('release-candidate')
    const relative = `network-CP11-${info.project.name}.json`
    const bytes = Buffer.from(JSON.stringify({ releases: metadata, transitions, events: rows }, null, 2))
    writeFileSync(join(run, relative), bytes)
    cp.result.network_log = { path: relative, sha256: hash(bytes) }
    save(info)
  }
})
