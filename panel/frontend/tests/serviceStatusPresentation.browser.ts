import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { service, diagnostics, render, sourceRoot } from './serviceStatusFixtures.ts'

test('390px下真实组件样式的服务卡、编号展开和诊断面板无横向溢出', async () => {
  const { chromium } = await import('@playwright/test')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } })
    const style = readFileSync(resolve(sourceRoot, 'style.css'), 'utf8')
    const screenshotDirectory = resolve(sourceRoot, '../../../staging/status-presentation-20261018')
    mkdirSync(screenshotDirectory, { recursive: true })
    for (const entry of ['pages/ServicesPage.vue', 'components/ServiceMetrics.vue', 'components/AdminUsageDiagnostics.vue']) {
      const props = entry.includes('Metrics') ? { service: service() } : entry.includes('Diagnostics') ? { data: diagnostics() } : {}
      const result = await render(entry, props)
      await page.setContent('<meta name="viewport" content="width=device-width,initial-scale=1"><style>' + style + '\n' + result.css + '</style><main style="padding:20px;min-width:0">' + result.html + '</main>')
      if (entry.includes('ServicesPage')) await page.locator('summary').click()
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, entry)
      await page.screenshot({ path: resolve(screenshotDirectory, entry.split('/').pop()!.replace('.vue', '-390.png')), fullPage: true })
    }
  } finally { await browser.close() }
})
