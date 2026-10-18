import { expect, test as base, type Page, type TestInfo } from '@playwright/test'
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'

export const run = process.env.ACCEPTANCE_RUN!
export const fixture = JSON.parse(readFileSync(join(run, 'fixture.json'), 'utf8'))
export const contract = JSON.parse(readFileSync(new URL('../../acceptance/critical-paths.json', import.meta.url), 'utf8'))
export const hash = (value: Buffer) => createHash('sha256').update(value).digest('hex')
export const results: any[] = []
export const test = base.extend({
  browser: async ({ browser }, use) => {
    const original=browser.newContext.bind(browser)
    browser.newContext=async options=>{
      const context=await original(options)
      await context.route('**/*',async route=>{
        const target=new URL(route.request().url())
        const permitted=new URL(process.env.ACCEPTANCE_BASE_URL!).origin
        if(target.origin===permitted || ['data:','blob:'].includes(target.protocol)) await route.fallback()
        else await route.abort('blockedbyclient')
      })
      return context
    }
    try {await use(browser)} finally {browser.newContext=original}
  },
})
export function scenario(id: string, info: TestInfo) {
  const definition = contract.required_scenarios.find((row: any) => row.id.startsWith(id + '-'))
  const result: any = { scenario_id: definition.id, viewport: info.project.name, status: 'NOT TESTED', assertions: [], screenshots: [] }
  results.push(result)
  return {
    result,
    async check(id: string, action: () => Promise<void>) {
      await action()
      result.assertions.push({ id, status: 'PASS' })
    },
    async shot(page: Page, checkpoint: string, options: { allowLoading?: boolean; preservePointer?: boolean } = {}) {
      if (!options.allowLoading) {
        await expect(page.locator('.el-skeleton:visible')).toHaveCount(0)
        await expect(page.locator('.el-loading-mask:visible')).toHaveCount(0)
      }
      if (checkpoint !== 'refresh-pending' && !options.allowLoading) {
        await expect(page.locator('.refresh-button[aria-busy="true"]:visible')).toHaveCount(0)
      }
      if (['rule-editor', 'line-usage', 'core-details', 'node-details', 'line-path', 'check-timeout', 'check-recovered', 'release-recovery', 'admin-metering-diagnostics'].includes(checkpoint)) {
        await expect(page.locator('.el-overlay:visible')).not.toHaveClass(/el-drawer-fade-(enter|leave)/)
        await expect(page.locator('.el-drawer:visible')).toBeInViewport({ ratio: 1 })
      }
      await page.evaluate(() => document.fonts.ready)
      const name = `${id}-${checkpoint}.png`
      const relative = `screenshots/${info.project.name}/${name}`
      mkdirSync(join(run, 'screenshots', info.project.name), { recursive: true })
      // 固定抽屉按真实视口取图；普通长页从顶部取全图，再恢复测试原滚动位置。
      const fullPage = !options.preservePointer && await page.locator('.el-overlay:visible').count() === 0
      const scroll = await page.evaluate(() => [window.scrollX, window.scrollY])
      try {
        if (fullPage && !options.preservePointer) await page.evaluate(() => window.scrollTo(0, 0))
        if (!options.preservePointer) await page.mouse.move(0, 0)
        // 首次候选也必须连续两帧一致；不把加载罩、悬停或滚动条淡出状态存成基线。
        let previous: Buffer | undefined, stable: Buffer | undefined
        for (let attempt = 0; attempt < 20; attempt++) {
          const current = await page.screenshot({ fullPage, animations: 'disabled' })
          if (previous?.equals(current)) { stable = current; break }
          previous = current
          await new Promise(resolve => setTimeout(resolve, 100))
        }
        if (!stable) throw new Error(`页面未在截止时间内稳定：${checkpoint}`)
        const bytes = stable
        writeFileSync(join(run, relative), bytes)
        result.screenshots.push({ checkpoint, path: relative, sha256: hash(bytes) })
        if (process.env.ACCEPTANCE_BOOTSTRAP === '1') {
          // 候选采图从不覆盖基线，整包保持待审，正常模式仍严格比对全部既有截图。
          result.visual_status = 'PENDING_REVIEW'
        } else if (existsSync(info.snapshotPath(name))) {
          await expect(page).toHaveScreenshot(name, { fullPage, animations: 'disabled', maxDiffPixels: 0 })
        } else {
          result.visual_status = 'PENDING_REVIEW'
          throw new Error(`缺少已审视觉基线：${info.snapshotPath(name)}`)
        }
      } finally {
        if (fullPage) await page.evaluate(([x, y]) => window.scrollTo(x, y), scroll)
      }
    },
    finish() {
      result.status = definition.assertions.every((a: any) => result.assertions.some((b: any) => a.id === b.id))
        && definition.screenshot_checkpoints.every((name: string) => result.screenshots.some((s: any) => s.checkpoint === name)) ? 'PASS' : 'NOT TESTED'
    },
  }
}
export function save(info: TestInfo) {
  const key = hash(Buffer.from(info.file + info.title)).slice(0,12)
  writeFileSync(join(run, `results-${info.project.name}-${key}.json`), JSON.stringify(results.splice(0), null, 2))
}
export function fixtureControl(operation: string, jobId?: string) {
  const command = [new URL('./fixture_runtime.py', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'), 'control', '--run', run, '--operation', operation]
  if (jobId) command.push('--job-id', jobId)
  return JSON.parse(execFileSync(process.env.ACCEPTANCE_PYTHON!, command, { encoding: 'utf8' }))
}
export function networkEvidence(page: Page, info: TestInfo, id: string) {
  const rows: any[] = []
  page.on('response', response => {
    const url = new URL(response.url())
    if (url.pathname.startsWith('/api/')) rows.push({ path: url.pathname, method: response.request().method(), status: response.status() })
  })
  return () => {
    const path = `network-${id}-${info.project.name}.json`
    const bytes = Buffer.from(JSON.stringify(rows, null, 2))
    writeFileSync(join(run, path), bytes)
    return { path, sha256: hash(bytes) }
  }
}
export async function login(page: Page, username = 'fixture-admin') {
  await page.goto('/app/#/login')
  await page.getByLabel('账号', { exact: true }).fill(username)
  await page.getByLabel('密码', { exact: true }).fill('Local-fixture-only-2026')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '我的服务', exact: true })).toBeVisible()
}
export async function adminNavigation(page: Page, label: string) {
  if (await page.getByRole('button', { name: '打开导航菜单' }).isVisible()) await page.getByRole('button', { name: '打开导航菜单' }).click()
  await page.getByRole('link', { name: label, exact: true }).filter({ visible: true }).first().click()
}
export async function noOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy()
}
