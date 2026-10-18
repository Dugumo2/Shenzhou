import test from 'node:test'
import assert from 'node:assert/strict'
import { service, diagnostics, render } from './serviceStatusFixtures.ts'

test('列表主标题仅服务名，完整编号进入资料并保原链接、额度、期限与应用状态', async () => {
  const { html } = await render('pages/ServicesPage.vue')
  assert.match(html, /<h2>原有套餐<\/h2>/)
  assert.doesNotMatch(html, /#a1b3da45|用量待核算|内部采样覆盖|迁入来源|已在线/)
  for (const value of ['服务资料', '服务编号', service().id, '/services/' + service().id, '98.25 GB', '1,000 GB', '901.74 GB', '2027/12/30', '2026/10/20', '已应用']) assert.ok(html.includes(value), value)
  assert.match(html, /<details class="service-information">/)
  assert.equal((html.match(/class="refresh-control/g) || []).length, 1)
})

test('旧服务响应保持状态兼容，真实统计失效仍显示简短提示', async () => {
  const old = service({ service_status: undefined, status_label: '已停用', business_state: 'disabled', usage: { quality: 'stale', message: '内部原因不展示', updated_at: null } })
  const { html } = await render('pages/ServicesPage.vue', {}, old)
  assert.match(html, /已停用/); assert.match(html, /用量更新延迟，请稍后更新/)
  assert.doesNotMatch(html, /内部原因不展示/)
  const empty = service({ usage_estimate: undefined, usage: { quality: 'unknown', message: '', updated_at: null } })
  assert.match((await render('pages/ServicesPage.vue', {}, empty)).html, /用量暂不可用，请稍后更新/)
})

test('服务资料保留额度、重置和到期并独立显示登记状态及编号', async () => {
  const { html } = await render('components/ServiceMetrics.vue', { service: service() })
  for (const value of ['1,000 GB', '98.25 GB', '901.74 GB', '2026/10/20', '2027/12/30', '已应用', '服务编号', service().id]) assert.ok(html.includes(value), value)
  assert.doesNotMatch(html, /用量待核算/)
  const compact = (await render('components/ServiceMetrics.vue', { service: service(), lifecycleOnly: true })).html
  assert.doesNotMatch(compact, /1,000 GB/); assert.match(compact, /到期时间/); assert.match(compact, /服务编号/)
})

test('管理员只呈现有限诊断字段，明确部分覆盖不等同实际断采', async () => {
  const data: any = diagnostics(); data.secret = 'SECRET-UNEXPECTED'; data.metering.token = 'TOKEN-UNEXPECTED'
  const { html } = await render('components/AdminUsageDiagnostics.vue', { data })
  for (const value of ['实际采样时刻', '2026/10/18 15:00', '部分覆盖', '不证明实际断采', '未标记（不代表完整覆盖）', '98.25 GB', '4 GB', '不分摊到历史时间图', '硬限额执行', '尚未验证']) assert.ok(html.includes(value), value)
  assert.doesNotMatch(html, /SECRET-UNEXPECTED|TOKEN-UNEXPECTED/)
  data.metering.reason_codes = ['SECRET-REASON']
  const unknown = (await render('components/AdminUsageDiagnostics.vue', { data })).html
  assert.match(unknown, /未识别的计量状态/); assert.doesNotMatch(unknown, /SECRET-REASON/)
  data.coverage.message_code = 'sampling_pending'
  assert.match((await render('components/AdminUsageDiagnostics.vue', { data })).html, /正常采样尾部不能直接判为断采/)
})

test('管理员首次加载不声称无记录，失败保旧诊断且可重试', async () => {
  const loading = (await render('components/AdminUsageDiagnostics.vue', { data: null, loading: true })).html
  assert.match(loading, /正在读取计量诊断/); assert.doesNotMatch(loading, /尚未读取计量诊断|0 GB/)
  const failed = (await render('components/AdminUsageDiagnostics.vue', { data: diagnostics(), error: '服务器内部路径不呈现' })).html
  assert.match(failed, /已保留上次结果，请重试/); assert.match(failed, /重试读取/); assert.match(failed, /98.25 GB/)
  assert.doesNotMatch(failed, /服务器内部路径不呈现/)
})
