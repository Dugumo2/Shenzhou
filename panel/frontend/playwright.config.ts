import { defineConfig } from '@playwright/test'

if (!process.env.ACCEPTANCE_RUN || !process.env.ACCEPTANCE_BASE_URL) {
  throw new Error('请通过 scripts/public/run_browser_acceptance.py 创建隔离环境后运行浏览器验收。')
}

export default defineConfig({
  testDir: './e2e', testMatch: '*.spec.ts', fullyParallel: false, workers: 1,
  timeout: 180_000, expect: { timeout: 12_000 }, retries: 0,
  forbidOnly: true, updateSnapshots: 'none',
  snapshotPathTemplate: '{testDir}/__screenshots__/{projectName}/{arg}{ext}',
  outputDir: process.env.ACCEPTANCE_RUN + '/test-results',
  reporter: [['list'], ['json', { outputFile: process.env.ACCEPTANCE_RUN + '/playwright.json' }]],
  use: { baseURL: process.env.ACCEPTANCE_BASE_URL, actionTimeout: 12_000, locale: 'zh-CN', timezoneId: 'Asia/Shanghai',
    colorScheme: 'light', reducedMotion: 'reduce', screenshot: 'only-on-failure', trace: 'off',
    browserName: 'chromium' },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 900 } } },
    { name: 'mobile', grepInvert: /@desktop/, use: { viewport: { width: 390, height: 844 } } },
  ],
})
