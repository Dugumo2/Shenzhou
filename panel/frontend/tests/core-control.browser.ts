import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { createServer } from 'vite'
import vuePlugin from '@vitejs/plugin-vue'
import { chromium } from '@playwright/test'
import { candidate, control, core, frontendRoot, job } from './coreControlFixtures.ts'

test('真实核心卡编译组件：候选检查失败恢复、UNKNOWN只读及宽窄屏', async () => {
  const artifacts = resolve(frontendRoot, '../../staging/core-control-ui-20261018')
  mkdirSync(artifacts, { recursive: true })
  const entry = '\0virtual:core-control-fixture'
  const server = await createServer({ configFile: false, root: frontendRoot, cacheDir: resolve(artifacts, 'vite-cache'),
    server: { host: '127.0.0.1', port: 0, strictPort: false }, optimizeDeps: { include: ['vue', 'element-plus'] },
    plugins: [vuePlugin(), { name: 'core-control-fixture',
      resolveId(id) { if (id === '/core-control-fixture-entry.ts') return entry },
      load(id) { if (id === entry) return `import { createApp } from 'vue';
        import ElementPlus from 'element-plus'; import 'element-plus/dist/index.css';
        import ServerCoreDetails from '/src/components/ServerCoreDetails.vue';
        import '/src/style.css';
        createApp(ServerCoreDetails, ${JSON.stringify({ cores: { items: [core()], truncated: false }, metadataEditable: false })}).use(ElementPlus).mount('#app');` },
      configureServer(instance) { instance.middlewares.use('/core-control-fixture', (_req, res) => {
        res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><html lang="zh"><meta name="viewport" content="width=device-width,initial-scale=1"><body><main id="app" style="max-width:1100px;margin:auto;padding:20px;min-width:0"></main><script type="module" src="/core-control-fixture-entry.ts"></script></body></html>')
      }) },
    }] })
  let browser: Awaited<ReturnType<typeof chromium.launch>> | undefined
  try {
    await server.listen()
    const address = server.httpServer!.address(); assert.ok(address && typeof address !== 'string')
    browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } })
    let latest = control({ candidates: [] }), checks = 0, writes = 0
    const errors: string[] = []; page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
    await page.route('**/api/v1/admin/cores/**', async route => {
      const request = route.request(), url = new URL(request.url())
      let data: any = latest
      if (request.method() === 'POST') {
        writes++
        if (url.pathname.endsWith('/candidates')) { latest = control(); data = { candidate: latest.candidates[0], replayed: false } }
        else if (url.pathname.endsWith('/check')) {
          checks++
          const passed = checks > 1
          latest = control({ candidates: [candidate({ state: passed ? 'checked' : 'check_failed', checked_at: passed ? '2026-10-18T11:02:00Z' : null,
            capabilities: { check: true, apply: passed, rollback: false } })], jobs: [job({ status: passed ? 'succeeded' : 'failed', error_code: passed ? '' : 'check_failed' })] })
          data = { job: latest.jobs[0], replayed: false }
        } else if (url.pathname.endsWith('/apply')) {
          latest = control({ ...latest, busy: true, jobs: [job({ action: 'apply', status: 'unknown', error_code: 'executor_interrupted' })] })
          data = { job: latest.jobs[0], replayed: false }
        } else throw new Error('未授权测试动作 ' + url.pathname)
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ data }) })
    })
    await page.goto(`http://127.0.0.1:${address.port}/core-control-fixture`)
    await page.waitForFunction(() => document.querySelectorAll('button').length > 0, undefined, { timeout: 10000 }).catch(async error => {
      await page.screenshot({ path: resolve(artifacts, 'fixture-load-failure.png'), fullPage: true })
      throw new Error('夹具未挂载：' + JSON.stringify(errors) + '；' + String(error))
    })
    await page.getByRole('button', { name: /现有 Xray/ }).click()
    const panel = page.getByRole('region', { name: '核心配置控制', exact: true })
    await panel.getByLabel('候选日志级别').selectOption('warning')
    await panel.getByRole('button', { name: '生成候选', exact: true }).click()
    await panel.getByRole('button', { name: '检查候选', exact: true }).click()
    await page.getByRole('region', { name: '核心执行任务' }).getByText('检查 · 执行失败', { exact: true }).waitFor()
    assert.equal(await panel.getByRole('button', { name: '应用候选', exact: true }).isDisabled(), true)
    assert.equal(latest.configuration_version, 'original-version')
    await panel.getByRole('button', { name: '检查候选', exact: true }).click()
    await panel.getByRole('checkbox', { name: '我已核对本候选差异及连接中断影响' }).check()
    await page.screenshot({ path: resolve(artifacts, 'core-control-1280-checked.png'), fullPage: true })
    await page.setViewportSize({ width: 390, height: 844 })
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    await page.screenshot({ path: resolve(artifacts, 'core-control-390-checked.png'), fullPage: true })
    await panel.getByRole('button', { name: '应用候选', exact: true }).click()
    await page.getByRole('region', { name: '核心执行任务' }).getByText('应用 · 结果未知', { exact: true }).waitFor()
    for (const name of ['生成候选', '检查候选', '应用候选']) assert.equal(await panel.getByRole('button', { name, exact: true }).isDisabled(), true)
    await panel.getByRole('button', { name: '更新执行状态', exact: true }).click()
    assert.equal(writes, 4)
    await page.screenshot({ path: resolve(artifacts, 'core-control-390-unknown.png'), fullPage: true })
    latest = control({ revision: 2, configuration_version: 'b'.repeat(64), current_applied_candidate_id: 'candidate-one',
      candidates: [candidate({ state: 'applied', capabilities: { check: false, apply: false, rollback: true } })],
      jobs: [job({ action: 'apply', status: 'succeeded', result: { config_sha256: 'b'.repeat(64), core_version: '25.1.0' } })] })
    await panel.getByRole('button', { name: '更新执行状态', exact: true }).click()
    await page.getByRole('region', { name: '核心执行任务' }).getByText('应用 · 执行成功', { exact: true }).waitFor()
    assert.equal(await panel.getByRole('button', { name: '回滚当前候选', exact: true }).isDisabled(), true)
    assert.equal(writes, 4); assert.deepEqual(errors, [])
  } finally { await browser?.close(); await server.close() }
})
