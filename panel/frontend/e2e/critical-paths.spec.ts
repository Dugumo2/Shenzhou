import { expect } from '@playwright/test'
import { test, scenario, save, fixture, login, adminNavigation, noOverflow, fixtureControl, run } from './helpers'
import { writeFileSync } from 'node:fs'
import { join } from 'node:path'

const choose = async (page: any, label: string) => page.locator('label').filter({ has: page.getByRole('radio', { name: label, exact: true }) }).click()
const gb = (value: string) => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(Number(value)/1e9) + ' GB'

test('CP01—CP07 同会话完整用户旅程', async ({ page, browser }, info) => {
  writeFileSync(join(run,'browser-version.json'),JSON.stringify({ browser_version:browser.version() }))
  await page.clock.setFixedTime(new Date(fixture.fixed_time))
  const cp1 = scenario('CP01', info)
  try {
    await page.goto('/app/#/login')
    await expect(page.getByLabel('账号', { exact: true })).toBeVisible()
    await cp1.shot(page, 'login')
    await cp1.check('CP01-A01', async () => {
      let posts = 0
      page.on('request', r => { if (r.url().endsWith('/api/v1/login') && r.method() === 'POST') posts++ })
      await login(page)
      await page.reload()
      await expect(page.locator('.service-card')).toHaveCount(1)
      expect(posts).toBe(1)
    })
    await cp1.check('CP01-A02', async () => {
      if(info.project.name==='desktop') {
        const persistent=page.locator('.shell-user-navigation').getByRole('link',{name:'管理面板',exact:true})
        await expect(persistent).toBeVisible();await persistent.click()
      } else await adminNavigation(page,'管理面板')
      await expect(page.getByText('我的套餐与流量', { exact: true })).toBeVisible()
      await cp1.shot(page, 'admin-home')
      await page.getByRole('link', { name: '进入我的服务', exact: true }).click()
      await expect(page.locator('.service-card')).toHaveCount(1)
    })
    await cp1.check('CP01-A04', async () => { await expect(page.locator('.shell-header')).toBeVisible(); await noOverflow(page) })
    await cp1.check('CP01-A03', async () => {
      for (const username of ['fixture-user','fixture-empty']) {
        const context=await browser.newContext({ viewport:info.project.use.viewport,locale:'zh-CN',timezoneId:'Asia/Shanghai',baseURL:process.env.ACCEPTANCE_BASE_URL })
        const other=await context.newPage();await other.clock.setFixedTime(new Date(fixture.fixed_time));await login(other,username)
        if (username==='fixture-user') { await expect(other.locator('.service-card')).toHaveCount(1);await expect(other.locator('.service-card')).toContainText(fixture.service_ids[username].slice(0,8)) }
        else { await expect(other.getByText('请联系管理员为你分配服务。',{exact:false})).toBeVisible();await expect(other.locator('.service-card')).toHaveCount(0);await expect(other.getByRole('link',{name:'先看看使用指南 →'})).toBeVisible() }
        await context.close()
      }
    })
    await cp1.shot(page, 'services')
    cp1.finish()
    const cp2 = scenario('CP02', info)
    const list = (await (await page.request.get('/api/v1/me/services')).json()).data.items
    const listed = list.find((row:any)=>row.id===fixture.service_ids['fixture-admin'])
    await page.getByRole('link', { name: /查看服务与使用方法/ }).click()
    await cp2.check('CP02-A02', async () => {
      await expect(page.locator('[data-chart="quota-gauge"]')).toBeVisible()
      await expect(page.locator('.quota-legend')).toContainText('1,000 GB')
      await expect(page.locator('.quota-legend')).toContainText(fixture.expected.charged_gb)
      await expect(page.locator('.quota-legend')).toContainText(fixture.expected.remaining_gb)
      const size = await page.locator('[data-chart="quota-gauge"]').boundingBox()
      expect(size!.width).toBeGreaterThan(100); expect(size!.height).toBeGreaterThan(100)
    })
    await cp2.check('CP02-A03', async () => {
      await expect(page.locator('[data-chart="transfer-composition"]')).toBeVisible();await expect(page.locator('.transfer-legend')).toContainText(fixture.expected.upload_gb);await expect(page.locator('.transfer-legend')).toContainText(fixture.expected.download_gb)
      for (const username of ['fixture-user','fixture-unknown']) {
        const context=await browser.newContext({viewport:info.project.use.viewport,locale:'zh-CN',timezoneId:'Asia/Shanghai',baseURL:process.env.ACCEPTANCE_BASE_URL})
        try {
          const other=await context.newPage();await other.clock.setFixedTime(new Date(fixture.fixed_time));await login(other,username);await other.getByRole('link',{name:/查看服务与使用方法/}).click()
          if(username==='fixture-user') {await expect(other.locator('.transfer-empty')).toContainText('已确认记录为零');await expect(other.locator('.quota-ring')).not.toHaveClass(/unknown/)}
          else {await expect(other.locator('.quota-ring')).toHaveClass(/unknown/);await expect(other.locator('.transfer-empty')).toContainText('等待可靠统计')}
        } finally {await context.close()}
      }
    })
    await cp2.check('CP02-A04', async () => { await expect(page.locator('.usage-dates')).toContainText('2026'); await expect(page.locator('.detail-summary')).toContainText('2027'); await expect(page.locator('[data-chart="usage-timeseries"]')).toHaveCount(0) })
    await cp2.check('CP02-A05', async () => { await expect(page.locator('main')).not.toContainText('采购预算'); await expect(page.locator('main')).not.toContainText('迁入') })
    await cp2.shot(page, 'service-overview')
    const cp3 = scenario('CP03', info)
    await page.getByRole('tab', { name: '流量用量', exact: true }).click()
    await expect(page.locator('[data-chart="usage-timeseries"]')).toBeVisible()
    const periodData:any[]=[]
    for (const [label, count, checkpoint, period] of [['近7天',7,'usage-7d','7d'],['本月',31,'usage-month','month'],['今年',12,'usage-year','year']] as const) {
      await page.locator('label').filter({ has: page.getByRole('radio', { name: label, exact: true }) }).click()
      await expect(page.locator('.timeline .chart-column')).toHaveCount(count)
      const data=(await (await page.request.get(`/api/v1/me/services/${listed.id}/usage?period=${period}`)).json()).data
      periodData.push(data)
      await expect(page.locator('.range-totals')).toContainText(gb(data.timeseries.totals.charged_bytes))
      await expect(page.locator('.range-totals')).toContainText(gb(data.timeseries.totals.upload_bytes))
      await expect(page.locator('.range-totals')).toContainText(gb(data.timeseries.totals.download_bytes))
      await expect(page.locator('.timeline')).toContainText('统计截至 2026/10/18 12:00')
      await cp3.shot(page, checkpoint)
    }
    await cp3.check('CP03-A01',async()=>{expect(periodData.map(row=>row.period)).toEqual(['7d','month','year']);await expect(page.locator('[data-chart="quota-gauge"]')).not.toBeVisible()})
    await cp3.check('CP03-A02',async()=>{expect(periodData[1].timeseries.totals.charged_bytes).not.toEqual(periodData[1].summary.charged_bytes)})
    await cp3.check('CP03-A03',async()=>{
      await expect(page.locator('.chart-column[data-state="future"]')).not.toHaveCount(0)
      await expect(page.locator('.missing-bar')).not.toHaveCount(0)
      await expect(page.locator('.chart-column[data-state="partial"]')).not.toHaveCount(0)
      await choose(page,'本月');await expect(page.locator('.actual-bar.zero')).not.toHaveCount(0)
    })
    await cp3.check('CP03-A04',async()=>{
      await choose(page,'原始上传');await page.locator('.timeline .chart-column').nth(3).click()
      await expect(page.locator('.chart-readout')).toContainText('10-04')
      await expect(page.locator('.chart-readout')).toContainText('0 GB')
      await choose(page,'套餐消耗');await noOverflow(page)
    })
    await cp3.check('CP03-A05',async()=>{
      await page.getByRole('button',{name:'查看线路使用明细'}).click()
      await expect(page.locator('.el-drawer')).toContainText('HOME 线路')
      await expect(page.locator('.el-drawer')).toContainText('BWH 线路')
      await expect(page.locator('.el-drawer')).toContainText('×1')
      const data=periodData[1];expect(data.line_usage.service_id).toBe(data.service_id);expect(data.line_usage.period).toBe('month');expect(data.line_usage.summary_revision).toBe(data.timeseries.summary_revision)
      await cp3.shot(page,'line-usage');await page.locator('.el-drawer__close-btn').click()
    })
    cp3.finish()
    const cp4 = scenario('CP04', info)
    await page.getByRole('tab', { name: '连接设置', exact: true }).click()
    await page.getByRole('button', { name: /电脑 Windows/ }).click()
    await page.locator('label').filter({ has: page.getByRole('radio', { name: 'Windows', exact: true }) }).click()
    await page.locator('label').filter({ has: page.getByRole('radio', { name: 'v2rayN', exact: true }) }).click()
    await expect(page.getByRole('heading', { name: 'v2rayN 连接设置' })).toBeVisible()
    const prepare=page.getByRole('button',{name:/准备演示文件|更新演示文件/,exact:true})
    await expect(prepare.or(page.locator('.delivery-resources .connection-steps'))).toBeVisible()
    if(await prepare.isVisible()) await prepare.click()
    await expect(page.locator('.delivery-resources .connection-steps')).toBeVisible()
    await cp4.shot(page, 'connections')
    await page.getByRole('link', { name: /查看 v2rayN 使用指南/ }).click()
    await expect(page.locator('.guide-content .guide-steps')).not.toHaveCount(0)
    await cp4.shot(page, 'guide')
    await cp4.check('CP04-A04',async()=>{
      const guides=(await (await page.request.get('/api/v1/catalog/guides')).json()).data
      expect(guides.total).toBe(11)
      await expect(page.locator('.guide-content')).toContainText('v2rayN')
      await page.getByLabel('搜索指南全文').fill('订阅');await page.getByRole('button',{name:'搜索',exact:true}).click();await expect(page.locator('.guide-content .guide-steps')).not.toHaveCount(0)
    })
    await adminNavigation(page,'我的服务')
    await page.getByRole('link',{name:/查看服务与使用方法/}).click();await page.getByRole('tab',{name:'连接设置',exact:true}).click()
    await cp4.check('CP04-A01',async()=>{
      await page.getByRole('button',{name:/手机 Android/}).click();await choose(page,'Android')
      for (const name of ['v2rayNG','sing-box / SFA']) {
        await choose(page,name);await expect(page.getByRole('heading',{name:`${name} 连接设置`})).toBeVisible()
        await page.getByRole('button',{name:'更换设备或软件'}).click()
      }
      expect(new URL(page.url()).hash).toContain(listed.id)
      const after=(await (await page.request.get('/api/v1/me/services')).json()).data.items
      expect(after.length).toBe(1);expect(after[0].used_bytes).toBe(listed.used_bytes)
    })
    await cp4.check('CP04-A03',async()=>{await page.getByRole('button',{name:/路由器 查看适配/}).click();await expect(page.getByText('路由器暂未提供经过验证的适配。')).toBeVisible();await expect(page.locator('.software-result:visible a[href*="download"]')).toHaveCount(0)})
    const p8Context=await browser.newContext({viewport:info.project.use.viewport,locale:'zh-CN',timezoneId:'Asia/Shanghai',baseURL:process.env.ACCEPTANCE_BASE_URL})
    try {
      const p8=await p8Context.newPage();await p8.clock.setFixedTime(new Date(fixture.fixed_time));await login(p8,'fixture-p8')
      await p8.getByRole('link',{name:/查看服务与使用方法/}).click()
      await cp2.check('CP02-A06',async()=>{
        const service=(await (await p8.request.get(`/api/v1/me/services/${fixture.service_ids['fixture-p8']}`)).json()).data
        expect(service.source_type).toBe('p8');await expect(p8.locator('[data-chart="quota-gauge"]')).toBeVisible();await expect(p8.locator('[data-chart="transfer-composition"]')).toBeVisible()
        await expect(p8.locator('.quota-legend')).toContainText(fixture.expected.remaining_gb);await expect(p8.locator('.detail-summary')).toContainText('2027/12/30')
      })
      await cp4.check('CP04-A02',async()=>{
        await p8.getByRole('tab',{name:'连接设置',exact:true}).click();await p8.getByRole('button',{name:/电脑 Windows/}).click();await choose(p8,'Windows');await choose(p8,'v2rayN')
        await expect(p8.locator('.p8-delivery-resources .connection-steps')).toBeVisible()
        await expect(p8.getByRole('button',{name:'复制地址',exact:true}).first()).toBeVisible()
        await expect(p8.getByRole('link',{name:'下载文件',exact:true}).first()).toHaveAttribute('href',/^https:\/\/fixture\.example\.invalid/)
        await expect(p8.locator('.p8-delivery-resources')).not.toContainText('连接已通过')
      })
    } finally { await p8Context.close() }
    cp2.finish();cp4.finish()
    await page.locator('.shell-account-menu summary').click()
    await page.locator('.shell-account-menu').getByRole('link', { name: '管理面板', exact: true }).click()
    await adminNavigation(page,'订阅管理')
    await cp2.check('CP02-A01',async()=>{
      const rows=(await (await page.request.get('/api/v1/admin/services')).json()).data.items
      const row=rows.find((r:any)=>r.id===listed.id)
      for (const key of ['quota_bytes','used_bytes','remaining_bytes','next_reset_at','expires_at']) expect(row[key]).toEqual(listed[key])
      const visible=page.locator('.el-table__row').filter({hasText:'fixture-admin'})
      await expect(visible).toContainText('1,000 GB');await expect(visible).toContainText(fixture.expected.charged_gb);await expect(visible).toContainText(fixture.expected.remaining_gb)
      await cp2.shot(page,'admin-subscription')
    })
    cp2.finish()
    const cp5 = scenario('CP05', info)
    await adminNavigation(page, '代理规则')
    await expect(page.getByRole('heading', { name: '代理规则', exact: true })).toBeVisible()
    await expect(page.locator('.el-table').last()).toContainText('example.com')
    await cp5.check('CP05-A01',async()=>{await expect(page.getByRole('button',{name:'添加规则'})).toBeVisible();await expect(page.getByLabel('搜索规则')).toBeVisible();await expect(page.locator('.el-switch').filter({has:page.getByRole('switch',{name:'启停规则 example.com'})})).toBeVisible();await expect(page.getByRole('link',{name:'规则来源',exact:true})).toBeVisible()})
    await cp5.check('CP05-A02',async()=>{
      await page.getByLabel('检查域名或网址').fill('www.example.com')
      const pending=page.waitForResponse(response=>response.url().endsWith('/api/v1/admin/rules/preview')&&response.request().method()==='POST')
      await page.getByRole('button',{name:'检查匹配'}).click()
      const response=await pending;expect(response.status()).toBe(200)
      const explanation=(await response.json()).data
      expect(explanation.final_action).toBe('proxy');expect(explanation.matched_id).toBe(fixture.rules.custom_id);expect(explanation.source_id).toBe('custom')
      const selected=explanation.related.find((row:any)=>row.id===fixture.rules.custom_id)
      expect(selected).toMatchObject({kind:'suffix',value:'example.com',order:fixture.rules.custom_id,source_id:'custom',action:'proxy',matched:true})
      expect(explanation.order_semantics).toBe('启用代理候选优先；其后保护校验，再本机直连候选；同动作按数据库ID')
      const current=(await(await page.request.get('/api/v1/admin/rules')).json()).data
      expect(explanation.candidate_revision).toBe(current.candidate_revision);expect(explanation.candidate_revision).toMatch(/^[a-f0-9]{64}$/)
      const shown=page.locator('.rule-inspect').locator('..')
      for(const value of ['当前代理',`命中规则 #${fixture.rules.custom_id}`,explanation.order_semantics,`记录版本：${explanation.candidate_revision.slice(0,12)}`,'来源：自建规则']) await expect(shown).toContainText(value)
      await expect(shown).toContainText('不会访问网址或检测连通性')
      await expect(page.locator('.el-table').last()).toContainText('域名及子域名')
      await cp5.shot(page,'rules')
      await page.getByRole('link',{name:'规则方案',exact:true}).click()
      await page.getByRole('button',{name:'查看与检查',exact:true}).click()
      await expect(page.getByRole('heading',{name:'验收固定方案 · v1',exact:true})).toBeVisible()
      const entries=page.locator('.el-table').nth(1).locator('.el-table__row')
      await expect(entries).toHaveCount(2)
      await expect(entries.nth(0)).toContainText('验收基础来源');await expect(entries.nth(0)).toContainText('work.example.net')
      await expect(entries.nth(1)).toContainText('自建规则');await expect(entries.nth(1)).toContainText('example.com')
      for(const [domain,order,source] of [['api.work.example.net',1,'验收基础来源'],['www.example.com',2,'自建规则']] as const) {
        await page.getByLabel('组合命中域名或网址').fill(domain)
        const checked=page.waitForResponse(response=>response.url().endsWith(`/api/v1/admin/rule-policies/${fixture.rules.policy_id}/preview`))
        await page.getByRole('button',{name:'检查当前版本',exact:true}).click()
        const answer=(await(await checked).json()).data
        expect(answer).toMatchObject({revision:1,sha256:fixture.rules.policy_sha256,final_action:'proxy',production_published:false,reason:'按组合顺序首次匹配'})
        expect(answer.matches).toHaveLength(1);expect(answer.matches[0]).toMatchObject({order,source_name:source,enabled:true,action:'proxy'})
        await expect(page.locator('main')).toContainText(`第${order}项 · ${source} · 当前代理 · 启用`)
        await expect(page.locator('main')).toContainText('这是方案v1对该主机的解释，尚不代表客户端应用或网站可访问')
      }
      await page.getByRole('link',{name:'自建规则',exact:true}).click()
      await expect(page.getByRole('heading',{name:'代理规则',exact:true})).toBeVisible()
    })
    await page.getByRole('button', { name: '编辑', exact: true }).first().click()
    await expect(page.getByLabel('规则内容', { exact: true })).toHaveValue('example.com')
    await cp5.shot(page, 'rule-editor')
    await cp5.check('CP05-A03',async()=>{
      await page.getByLabel('规则内容',{exact:true}).fill('not a domain');await page.getByRole('button',{name:'保存候选',exact:true}).click();await expect(page.locator('.el-drawer .el-alert')).toBeVisible()
      let previous=(await(await page.request.get('/api/v1/admin/rules')).json()).data.items.find((row:any)=>row.id===fixture.rules.custom_id)
      for(const value of ['review.example.com','example.com']) {
        if(value==='example.com') await page.getByRole('button',{name:'编辑',exact:true}).first().click()
        await page.getByLabel('规则内容',{exact:true}).fill(value)
        const saved=page.waitForResponse(response=>response.url().endsWith(`/api/v1/admin/rules/${fixture.rules.custom_id}`)&&response.request().method()==='PATCH')
        await page.getByRole('button',{name:'保存候选',exact:true}).click()
        const response=await saved;expect(response.status()).toBe(200)
        const receipt=(await response.json()).data
        expect(receipt.item).toMatchObject({id:fixture.rules.custom_id,value,revision:previous.revision+1});expect(receipt.replayed).toBe(false)
        await expect(page.locator('.el-drawer')).not.toBeVisible()
        await expect(page.locator('.el-table').last().getByText(value,{exact:true})).toBeVisible()
        const listing=(await(await page.request.get('/api/v1/admin/rules')).json()).data
        const stored=listing.items.find((row:any)=>row.id===fixture.rules.custom_id)
        expect(stored.value).toBe(value);expect(stored.revision).toBe(receipt.item.revision);expect(listing.candidate_revision).toBe(receipt.candidate_revision)
        previous=stored
      }
      await expect(page.locator('main')).not.toContainText('已发布成功')
    })
    await cp5.check('CP05-A04',async()=>{
      await page.getByRole('link',{name:'规则来源',exact:true}).click()
      await expect(page.getByRole('heading',{name:'规则来源',exact:true})).toBeVisible()
      await expect(page.locator('.el-table__row')).toContainText('验收基础来源');await expect(page.locator('.el-table__row')).toContainText('v2')
      const current=page.waitForResponse(response=>response.url().includes(`/api/v1/admin/rule-sources/${fixture.rules.source_id}?`))
      await page.getByRole('button',{name:'查看版本',exact:true}).click()
      const latest=(await(await current).json()).data
      expect(latest.version).toMatchObject({revision:2,sha256:fixture.rules.source_versions['2'],count:1})
      const sourceDrawer=page.getByRole('dialog',{name:'来源版本',exact:true})
      await expect(sourceDrawer).toContainText('当前 v2 · 查阅 v2');await expect(sourceDrawer).toContainText('updated.example.net')
      await sourceDrawer.locator('.el-select').click()
      const historical=page.waitForResponse(response=>response.url().includes(`/api/v1/admin/rule-sources/${fixture.rules.source_id}?version=1`))
      await page.getByRole('option',{name:'v1 · 1条',exact:true}).click()
      const old=(await(await historical).json()).data
      expect(old.version).toMatchObject({revision:1,sha256:fixture.rules.source_versions['1'],count:1})
      await expect(sourceDrawer).toContainText('当前 v2 · 查阅 v1');await expect(sourceDrawer).toContainText('work.example.net')
      await expect(sourceDrawer).toContainText('域名及子域名');await expect(sourceDrawer).toContainText('当前代理')
      await sourceDrawer.locator('.el-drawer__close-btn').click()
      await page.getByRole('link',{name:'规则方案',exact:true}).click();await page.getByRole('button',{name:'查看与检查',exact:true}).click()
      const policy=(await(await page.request.get(`/api/v1/admin/rule-policies/${fixture.rules.policy_id}`)).json()).data
      expect(policy.version.sha256).toBe(fixture.rules.policy_sha256);expect(policy.version.document.references[0]).toMatchObject({revision:1,sha256:fixture.rules.source_versions['1']})
      await expect(page.locator('.el-table').nth(1)).toContainText('work.example.net');await expect(page.locator('.el-table').nth(1)).not.toContainText('updated.example.net')
      const context=await browser.newContext({viewport:info.project.use.viewport,locale:'zh-CN',timezoneId:'Asia/Shanghai',baseURL:process.env.ACCEPTANCE_BASE_URL})
      try {
        const ordinary=await context.newPage();await ordinary.clock.setFixedTime(new Date(fixture.fixed_time));await login(ordinary,'fixture-user')
        for(const route of ['/admin/rules','/admin/rule-sources','/admin/rule-policies']) expect((await ordinary.request.get('/api/v1'+route)).status()).toBe(403)
        for(const route of ['/admin/rules','/admin/rules/sources','/admin/rule-policies']) {
          await ordinary.goto('/app/#'+route);await expect(ordinary.getByRole('heading',{name:'我的服务',exact:true})).toBeVisible()
          for(const name of ['添加规则','导入规则文件','新建方案','编辑为下一版本']) await expect(ordinary.getByRole('button',{name,exact:true})).toHaveCount(0)
        }
      } finally {await context.close()}
    })
    cp5.finish()
    const cp6 = scenario('CP06', info)
    await adminNavigation(page, '服务器')
    await cp6.check('CP06-A01', async () => {
      await expect(page.locator('.server-grid')).toContainText('验收服务器')
      await expect(page.locator('.server-grid').getByText('尚无有效采样',{exact:true})).toBeVisible()
      await expect(page.locator('.server-grid').getByText('暂无可信样本',{exact:false})).toBeVisible()
      for (const value of ['21.5','42.5','31.2']) await expect(page.locator('.server-grid')).toContainText(value)
      await page.getByRole('button', { name: '紧凑列表', exact: true }).click()
      await expect(page.locator('.server-grid')).toHaveClass(/is-compact/)
      await page.getByRole('button', { name: '卡片', exact: true }).click()
    })
    await cp6.shot(page, 'server-cards')
    await page.getByRole('button', { name: '验收服务器', exact: true }).click()
    const drawer = page.locator('.el-drawer')
    await drawer.getByRole('button', { name: '核心', exact: true }).click()
    await expect(drawer).toContainText('验收 sing-box 核心')
    await drawer.getByRole('button',{name:/验收 sing-box 核心/}).click()
    await cp6.check('CP06-A02',async()=>{for(const text of ['1.12.0','隔离验收','运行控制尚未接管']) await expect(drawer).toContainText(text);for(const name of ['概览','核心','节点','用量与有效期','检查记录']) await expect(drawer.getByRole('button',{name,exact:true})).toBeVisible()})
    await cp6.shot(page, 'core-details')
    await drawer.getByRole('button', { name: '节点', exact: true }).click()
    await drawer.locator('summary').filter({ hasText:'HOME 外部节点' }).click()
    await expect(drawer).toContainText('Shadowsocks 2022')
    await cp6.check('CP06-A03',async()=>{for(const text of ['外部资源由供应商维护','本机 SOCKS 中间节点','HOME 线路入口','执行服务器']) await expect(drawer).toContainText(text)})
    await cp6.shot(page, 'node-details')
    await cp6.check('CP06-A05',async()=>{await expect(drawer.getByRole('link',{name:'查看关联线路 →'})).toBeVisible();await drawer.getByRole('button',{name:'用量与有效期',exact:true}).click();await expect(drawer.getByRole('heading',{name:'用量与有效期'})).toBeVisible();await drawer.getByRole('button',{name:'节点',exact:true}).click()})
    await cp6.check('CP06-A04',async()=>{
      fixtureControl('observations_stale')
      try {
        await drawer.getByRole('button',{name:'概览',exact:true}).click();await drawer.getByRole('button',{name:'更新数据',exact:true}).click()
        await expect(drawer).toContainText('观测已过期');await expect(drawer.locator('dl div').filter({has:page.locator('dt',{hasText:'接收速率'})})).toContainText('未知')
        await expect(drawer).toContainText('采样 2026/10/18 11:59')
      } finally {fixtureControl('observations_restore')}
      await drawer.getByRole('button',{name:'更新数据',exact:true}).click();await drawer.getByRole('button',{name:'节点',exact:true}).click()
    })
    cp6.finish()
    const cp7 = scenario('CP07', info)
    await drawer.getByRole('link', { name: '查看关联线路 →', exact: true }).click()
    await expect(page.locator('.association-filter')).toBeVisible()
    await expect(page.locator('.line-card-grid')).toContainText('HOME 线路')
    await page.getByRole('button', { name: 'HOME 线路', exact: true }).click()
    await expect(page.locator('.el-drawer')).toContainText('HOME 外部节点')
    await cp7.check('CP07-A01',async()=>{
      expect(page.url()).toContain('server_id='+fixture.server_id);await expect(page.locator('.association-filter')).toBeVisible();await expect(page.locator('.line-card-grid .line-card')).toHaveCount(2)
      await page.locator('.el-drawer__close-btn').click();await page.getByRole('button',{name:'紧凑列表',exact:true}).click();await expect(page.locator('.el-table__row')).toHaveCount(2);expect(page.url()).toContain('server_id='+fixture.server_id)
      await page.getByRole('button',{name:'卡片',exact:true}).click();await adminNavigation(page,'线路');await expect(page.getByRole('heading',{name:'线路',exact:true})).toBeVisible();await page.getByRole('button',{name:'HOME 线路',exact:true}).click()
    })
    await cp7.check('CP07-A02',async()=>{for(const text of ['HOME 线路入口','验收 sing-box 核心','本机 SOCKS 中间节点','HOME 外部节点']) await expect(page.locator('.el-drawer .line-path-diagram')).toContainText(text);expect(await page.locator('.el-drawer .path-segment').count()).toBeGreaterThanOrEqual(2)})
    await cp7.check('CP07-A03',async()=>{for(const text of ['TCP','UDP','DNS','NOT TESTED','已过期','2026/10/18 11:50']) await expect(page.locator('.el-drawer')).toContainText(text);await expect(page.locator('.el-drawer')).not.toContainText('整链通过（有效期内）')})
    await cp7.check('CP07-A04',async()=>{await expect(page.locator('.el-drawer')).toContainText('住宅或住宅 DNS 失败须关闭路径');await expect(page.locator('.el-drawer')).toContainText('目前登记信息不能证明这些行为已经生效');await expect(page.locator('.el-drawer')).not.toContainText('自动直出')})
    await cp7.shot(page, 'line-path')
    cp7.finish()
    writeFileSync(join(run,`journey-${info.project.name}.json`),JSON.stringify({status:'PASS',session_id:info.project.name+'-'+info.testId,scenario_ids:['CP01-login-home','CP02-service-overview','CP03-usage-periods','CP04-connections-guides','CP05-rules','CP06-server-core-node','CP07-lines']}))
  } finally { save(info) }
})
