import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { chromium } from '@playwright/test'
import { compiled, css, plan, refreshCode, displayCode } from './resourcePlanFixtures.ts'

test('真实编译采购组件在390px与宽屏可预览、悬浮、冲突保输入且无横向溢出', async () => {
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } })
    await page.route('http://127.0.0.1:17866/**', route => route.fulfill({ contentType: 'text/html', body: '<meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:0;background:#f5f7fb;font-family:Arial,sans-serif}main{padding:16px;max-width:1000px;margin:auto;box-sizing:border-box}' + css + '</style><main><div id="app"></div></main>' }))
    await page.goto('http://127.0.0.1:17866/')
    await page.addScriptTag({ path: resolve('node_modules/vue/dist/vue.global.prod.js') })
    // 浏览器运行真实组件；仅该组件API使用隔离夹具，整批真实Django路径由独立验收单覆盖。
    await page.addScriptTag({ content: `
      window.purchaseWrites = [];
      const initial = ${JSON.stringify(plan())};
      let reads = 0;
      class FixtureError extends Error { constructor(status, code, message) { super(message); this.status=status; this.code=code; } }
      const fakeApi = { ApiError: FixtureError, errorMessage: e => e.message, request: async (path, method, body) => {
        if (method !== 'POST') {
          const revision=++reads;
          if (window.fixtureQuotaConsumed) return {...initial,revision,plan:{...initial.plan,quota_bytes:'500000000000',cycle:{kind:'fixed_days',days:31,anchor:'2026-10-17T00:00:00+08:00'}},effective_plan:{...initial.effective_plan,quota_bytes:'500000000000'},consumption:{status:'consumed',revision,consumed_at:'2026-10-18T11:30:00Z'}};
          return {...initial, revision, ...(window.fixtureRejected ? {consumption:{status:'rejected',revision,consumed_at:'2026-10-18T11:30:00Z',error_code:'cycle_reset_expired'}} : {})};
        }
        window.purchaseWrites.push(JSON.parse(JSON.stringify(body)));
        if (path.endsWith('/confirm')) throw new FixtureError(409,'revision_conflict','采购计划已变化');
        const after = JSON.parse(JSON.stringify(initial.plan));
        if (body.operation === 'quota') after.quota_bytes = body.changes.quota_bytes;
        if (body.operation === 'expiry') after.expires_on = body.changes.expires_on;
        if (body.operation === 'cycle') after.cycle = {kind:'fixed_days',days:body.changes.days,anchor:body.changes.next_reset_at};
        return {...initial,revision:body.expected_revision,operation:body.operation,changes:body.changes,before:initial.plan,after,preview_token:'fixture-token',effects:['只影响未来计划，既有用量和用户套餐保持不变。']};
      }};
      const displayExports = {}; ((exports) => { ${displayCode} })(displayExports);
      const refreshExports = {}; ((exports, require) => { ${refreshCode} })(refreshExports, name => name === 'vue' ? Vue : displayExports);
      const exports = {}; const require = name => name === 'vue' ? Vue : name === '../api' ? fakeApi : refreshExports;
      ${compiled(true)}
      const app = Vue.createApp(exports.default,{bindingKey:'fixture-binding',enabled:true});
      app.component('ElButton',{setup:(_,ctx)=>()=>Vue.h('button',ctx.attrs,ctx.slots.default?.())});
      app.mount('#app');
    ` })
    await page.getByLabel('采购额度（GB）', { exact: true }).fill('500')
    await page.getByRole('button', { name: '预览变更', exact: true }).click()
    await page.getByRole('region', { name: '采购变更预览' }).waitFor()
    await page.getByRole('button', { name: '确认保存采购计划', exact: true }).hover()
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    const screenshots = resolve('../../staging/resource-plan-ui-20261018'); mkdirSync(screenshots, { recursive: true })
    await page.screenshot({ path: resolve(screenshots, 'quota-preview-390.png'), fullPage: true })
    await page.getByRole('button', { name: '确认保存采购计划', exact: true }).click()
    await page.getByRole('button', { name: '读取当前版本，保留输入', exact: true }).click()
    assert.equal(await page.getByLabel('采购额度（GB）', { exact: true }).inputValue(), '500')
    assert.equal(await page.evaluate(() => (window as any).purchaseWrites.length), 2)
    await page.getByRole('button', { name: '采用当前版本并重新预览', exact: true }).click()
    assert.equal(await page.getByLabel('采购额度（GB）', { exact: true }).inputValue(), '500')
    await page.getByRole('button', { name: '重置周期', exact: true }).click()
    await page.getByLabel('周期天数', { exact: true }).fill('31')
    await page.getByLabel('下次重置时间（上海）', { exact: true }).fill('2026-11-19T00:00')
    await page.getByRole('button', { name: '预览变更', exact: true }).click()
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    await page.screenshot({ path: resolve(screenshots, 'cycle-preview-390.png'), fullPage: true })
    await page.setViewportSize({ width: 1280, height: 900 })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    await page.screenshot({ path: resolve(screenshots, 'cycle-preview-wide.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.evaluate(() => { (window as any).fixtureRejected = true })
    await page.getByRole('button', { name: '读取计划状态', exact: true }).click()
    await page.getByRole('button', { name: '重新选择重置时间', exact: true }).waitFor()
    assert.match(await page.locator('.consumption').innerText(), /重置时间已过，请重新选择/)
    assert.doesNotMatch(await page.locator('.consumption').innerText(), /尚待采集流程读取|已生效/)
    await page.getByLabel('下次重置时间（上海）', { exact: true }).fill('2020-01-01T00:00')
    await page.getByRole('button', { name: '重新选择重置时间', exact: true }).click()
    assert.equal(await page.getByLabel('下次重置时间（上海）', { exact: true }).inputValue(), '')
    await page.getByLabel('下次重置时间（上海）', { exact: true }).fill('2028-01-20T00:00')
    await page.getByRole('button', { name: '预览变更', exact: true }).click()
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    await page.screenshot({ path: resolve(screenshots, 'rejected-cycle-recovery-390.png'), fullPage: true })
    await page.evaluate(() => { (window as any).fixtureQuotaConsumed = true })
    await page.getByRole('button', { name: '读取计划状态', exact: true }).click()
    const effective = page.getByLabel('当前有效采购计划', { exact: true })
    await effective.getByText('500 GB', { exact: true }).waitFor()
    assert.match(await effective.innerText(), /每 30 天/); assert.doesNotMatch(await effective.innerText(), /每 31 天/)
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    await page.screenshot({ path: resolve(screenshots, 'quota-after-rejected-cycle-390.png'), fullPage: true })
  } finally { await browser.close() }
})
