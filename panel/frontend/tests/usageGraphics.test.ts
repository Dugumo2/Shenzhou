import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse, compileScript } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import type { UsageOverviewData } from '../src/usage-types.ts'
import type { ResourceUsageMeter } from '../src/providerUsage.ts'

const require = createRequire(import.meta.url)
const sourceRoot = fileURLToPath(new URL('../src/', import.meta.url))

function overview(): UsageOverviewData {
  return {
    service_id: 'graphics-fixture', source_type: 'entitlement', time_zone: 'Asia/Shanghai',
    generated_at: '2026-10-03T01:00:00Z', period: 'current',
    current_cycle: { starts_at: '2026-09-30T16:00:00Z', ends_at: '2026-10-31T16:00:00Z' },
    summary: { quota_bytes: '100000000000', quota_state: 'applied', charged_bytes: '30000000000', remaining_bytes: '70000000000', upload_bytes: '10000000000', download_bytes: '20000000000', next_reset_at: '2026-10-31T16:00:00Z' },
    quality: { state: 'measured', message: '合成样本', collected_at: '2026-10-03T00:59:00Z' },
    history: { kind: 'confirmed_ledger_postings', date_basis: 'created_at', range_start: '2026-09-30T16:00:00Z', range_end: '2026-10-31T16:00:00Z', totals: { charged_bytes: '30000000000', upload_bytes: '10000000000', download_bytes: '20000000000' }, record_count: 2, excluded_record_count: 0, day_limit: 90, days_truncated: false, message: '', days: [
      { date: '2026-10-01', record_count: 1, charged_bytes: '30000000000', upload_bytes: '10000000000', download_bytes: '20000000000' },
      { date: '2026-10-03', record_count: 1, charged_bytes: '0', upload_bytes: '0', download_bytes: '0' },
    ] },
  }
}

function meter(changes: Partial<ResourceUsageMeter> = {}): ResourceUsageMeter {
  return { id: 'resource-one', label: '资源甲', scope: 'external_node', source_kind: 'estimate', quality: 'current',
    quota_bytes: '400000000000', used_bytes: '73000000000', remaining_bytes: '327000000000', upload_bytes: '23000000000', download_bytes: '50000000000',
    observed_at: '2026-10-07T01:00:00Z', expires_at: '2099-10-07T02:00:00Z',
    cycle: { kind: 'fixed_days', starts_at: '2026-09-20T00:00:00+08:00', ends_at: '2026-10-20T00:00:00+08:00', next_reset_at: '2026-10-20T00:00:00+08:00' }, expires_on: '2027-01-19', alerts: [], details: {}, ...changes }
}

// 编译并渲染真实概览及其真实子组件，仅替换接口响应；不复制模板或重写来源分支。
async function renderEntry(entry: string, props: Record<string, unknown>, responses: Record<string, unknown>) {
  const cache = new Map<string, any>(), requests: string[] = []
  const auth = vue.reactive({ session: { authenticated: true, user: { username: 'graphics-user', is_staff: false }, csrf_token: 'fixture-token' } })
  function load(filename: string): any {
    if (cache.has(filename)) return cache.get(filename)
    const source = readFileSync(filename, 'utf8')
    const content = filename.endsWith('.vue')
      ? compileScript(parse(source).descriptor, { id: filename, inlineTemplate: true }).content : source
    const code = ts.transpileModule(content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
    const exports: Record<string, any> = {}
    cache.set(filename, exports)
    new Function('require', 'exports', code)((name: string) => {
      if (name === 'vue') return vue
      if (/\/api(?:\.ts)?$/.test(name)) return { ...api, request: async (path: string) => {
        requests.push(path)
        assert.ok(path in responses, '未预期的读取：' + path)
        return responses[path]
      } }
      if (/\/auth(?:\.ts)?$/.test(name)) return { auth }
      if (name === 'vue-router') return { useRoute: () => ({ params: { id: 'graphics-fixture' } }) }
      if (!name.startsWith('.')) return require(name)
      return load(resolve(dirname(filename), /\.(vue|ts)$/.test(name) ? name : name + '.ts'))
    }, exports)
    return exports
  }
  const component = load(resolve(sourceRoot, entry)).default
  const wrapper = { ...component, async setup(props: any, context: any) {
    const render = component.setup(props, context)
    for (let i = 0; i < 40; i++) await Promise.resolve()
    return render
  } }
  const app = vue.createSSRApp(wrapper, props)
  for (const name of ['el-button', 'el-alert', 'el-skeleton', 'el-tag', 'el-radio-group', 'el-radio-button', 'el-tabs', 'el-tab-pane', 'el-progress', 'el-drawer']) {
    app.component(name, { setup(_props: unknown, { slots }: any) { return () => vue.h('div', slots.default?.()) } })
  }
  app.component('RouterLink', { props: ['to'], setup(props: any, { slots }: any) { return () => vue.h('a', { href: props.to }, slots.default?.()) } })
  const html = await renderToString(app)
  return { html, requests }
}

async function renderOverview(data: UsageOverviewData, summaryOnly = false) {
  const { html, requests } = await renderEntry('components/UsageOverview.vue', { serviceId: data.service_id, summaryOnly }, { '/me/services/graphics-fixture/usage?period=7d': data })
  assert.deepEqual(requests, ['/me/services/graphics-fixture/usage?period=7d'])
  return html
}

function temporal(data: UsageOverviewData, period: '7d' | 'month' | 'year' = '7d') {
  const values = period === 'year' ? [null,null,null,null,null,null,null,null,'60000000000','40000000000',null,null] : ['0','4000000000','6000000000','0','10000000000','8000000000','12000000000']
  data.period = period
  data.timeseries = { service_id:data.service_id, summary_revision:'r1', time_zone:'Asia/Shanghai', period, granularity:period==='year'?'month':'day', range_start:'2026-10-01T00:00:00+08:00', range_end:'2026-10-07T15:00:00+08:00', as_of:'2026-10-07T15:00:00+08:00', totals:{ state:period==='year'?'partial':'complete', charged_bytes:period==='year'?'100000000000':'40000000000',upload_bytes:'12000000000',download_bytes:'18000000000',unallocated_charged_bytes:'0' }, buckets:values.map((value,index)=>({ start:period==='year'?`2026-${String(index+1).padStart(2,'0')}-01T00:00:00+08:00`:`2026-10-${String(index+1).padStart(2,'0')}T00:00:00+08:00`,end:'2026-10-08T00:00:00+08:00',state:value===null?(index>9?'future':'missing'):'complete',is_open:index===(period==='year'?9:6),covered_through:null,charged_bytes:value,upload_bytes:value,download_bytes:value })),unallocated:[],unallocated_next_cursor:null,boundary_pending_count:0,message_code:null }
  return data
}

test('用户概览忽略旧provider原件，仍保留一份套餐圆环和原始上下行构成', async () => {
  const data = temporal(overview()); data.source_type='p8'
  data.provider_usage={schema_version:2,generated_at:data.generated_at,meters:[meter(),meter({id:'two',label:'资源乙'})]}
  const html=await renderOverview(data,true)
  assert.equal((html.match(/data-chart="quota-gauge"/g)||[]).length,1)
  assert.equal((html.match(/data-chart="transfer-composition"/g)||[]).length,1)
  assert.match(html,/100 GB/); assert.match(html,/30 GB/); assert.match(html,/70 GB/)
  assert.doesNotMatch(html,/资源甲|资源乙|统计详情|details|400 GB/)
})

test('用量真实组件只有时间柱图和范围总计，已测零与缺失及未来分别表达', async () => {
  const data=temporal(overview()), html=await renderOverview(data)
  assert.match(html,/data-chart="usage-timeseries"/)
  assert.doesNotMatch(html,/data-chart="quota-gauge"|data-chart="transfer-composition"/)
  assert.equal((html.match(/data-state="complete"/g)||[]).length,7)
  assert.match(html,/2026-10-01：0 GB/); assert.match(html,/40 GB/); assert.match(html,/查看线路使用明细/)
  const yearly=temporal(overview(),'year')
  const year=(await renderEntry('components/UsageTimeline.vue',{data:yearly,period:'year'},{})).html
  assert.equal((year.match(/data-state=/g)||[]).length,12)
  assert.equal((year.match(/data-state="missing"/g)||[]).length,8)
  assert.equal((year.match(/data-state="future"/g)||[]).length,2)
  assert.match(year,/所选范围已记录/); assert.match(year,/100 GB/)
  assert.doesNotMatch(year,/所选范围合计/)
})

test('未知历史保留完整日历图，不用旧created_at记录伪造日期用量',async()=>{
  const data=overview(),html=await renderOverview(data)
  assert.match(html,/data-chart="usage-timeseries"/)
  assert.equal((html.match(/data-state="missing"/g)||[]).length,7)
  assert.doesNotMatch(html,/class="actual-bar|class="quota-ring/)
  const compact=await renderOverview(data,true)
  assert.match(compact,/data-chart="quota-gauge"/);assert.match(compact,/data-chart="transfer-composition"/)
})

test('真实服务父页与列表使用统一P8套餐，保留固定三分区、软件向导与一次用量读取',async()=>{
  const data=temporal(overview());data.source_type='p8';data.provider_usage={schema_version:2,generated_at:data.generated_at,meters:[meter()]}
  const service={id:data.service_id,name:'神舟云',source_type:'p8',provider_usage:data.provider_usage,quota_bytes:'100000000000',used_bytes:'30000000000',raw_bytes:'30000000000',remaining_bytes:'70000000000',next_reset_at:'2026-11-01T00:00:00+08:00',expires_at:'2027-01-19T00:00:00+08:00',state:'active',enabled:true,status_label:'使用中',business_state:'active',quota_state:'applied',usage:{quality:'measured',updated_at:data.generated_at,message:''},delivery:{state:'unknown',message:'',download_url:null}}
  const responses={'/me/services/graphics-fixture':service,'/me/services/graphics-fixture/usage?period=7d':data,'/catalog/clients':{items:[]},'/me/services':{items:[service],compatibility:{state:'ready',message:null}}}
  const detail=await renderEntry('pages/ServicePage.vue',{},responses)
  assert.deepEqual([...detail.requests].sort(),['/catalog/clients','/me/services/graphics-fixture','/me/services/graphics-fixture/usage?period=7d'])
  assert.equal((detail.html.match(/data-chart="quota-gauge"/g)||[]).length,1)
  assert.equal((detail.html.match(/data-chart="usage-timeseries"/g)||[]).length,1)
  assert.equal((detail.html.match(/data-chart="transfer-composition"/g)||[]).length,1)
  assert.equal((detail.html.match(/class="refresh-control/g)||[]).length,1)
  assert.equal((detail.html.match(/下次流量重置/g)||[]).length,1)
  for(const text of ['本期流量周期','到期时间','服务状态'])assert.ok(detail.html.includes(text),text)
  for(const text of ['服务概览','流量用量','连接设置','手机','电脑','路由器'])assert.ok(detail.html.includes(text),text)
  const list=await renderEntry('pages/ServicesPage.vue',{},responses)
  assert.match(list.html,/href="\/services\/graphics-fixture"/)
  for(const html of [detail.html,list.html]){for(const text of ['100 GB','30 GB','70 GB','2027'])assert.ok(html.includes(text),text);assert.doesNotMatch(html,/资源甲|按资源分别计算|400 GB/)}
})

test('线路明细沿用所选范围与水位，倍率分段保留且不能显示旧月份数字',async()=>{
  const data=temporal(overview(),'year'),t=data.timeseries!
  data.line_usage={service_id:data.service_id,time_zone:'Asia/Shanghai',period:'year',range_start:t.range_start,range_end:t.range_end,as_of:t.as_of,summary_revision:'r1',totals:t.totals,boundary_pending_count:0,message_code:null,lines:[{line_id:'line-public',line_name:'本人授权线路',node_name:'节点甲',multiplier:'2',effective_from:'2026-09-01T00:00:00+08:00',effective_to:'2026-10-01T00:00:00+08:00',upload_bytes:'12000000000',download_bytes:'18000000000',charged_bytes:'60000000000'}]}
  const correct=(await renderEntry('components/UsageTimeline.vue',{data,period:'year'},{})).html
  assert.match(correct,/本人授权线路/);assert.match(correct,/×2/);assert.match(correct,/60 GB/)
  data.line_usage.period='month'
  const wrong=(await renderEntry('components/UsageTimeline.vue',{data,period:'year'},{})).html
  assert.doesNotMatch(wrong,/本人授权线路|×2/);assert.match(wrong,/暂无可确认的线路明细/)
  data.line_usage.period='year';data.line_usage.summary_revision='older'
  const stale=(await renderEntry('components/UsageTimeline.vue',{data,period:'year'},{})).html
  assert.doesNotMatch(stale,/本人授权线路|×2/)
})

test('月中重置后概览本期与自然月范围各读服务端值，未分桶仅计入总量一次',async()=>{
  const data=temporal(overview(),'month');data.summary.charged_bytes='10000000000';data.summary.remaining_bytes='90000000000'
  data.timeseries!.totals.charged_bytes='42000000000';data.timeseries!.totals.unallocated_charged_bytes='2000000000'
  data.timeseries!.unallocated=[{starts_at:'2026-10-06T23:59:00+08:00',ends_at:'2026-10-07T00:01:00+08:00',charged_bytes:'2000000000',upload_bytes:'0',download_bytes:'2000000000'}]
  const summary=await renderOverview(data,true),month=(await renderEntry('components/UsageTimeline.vue',{data,period:'month'},{})).html
  assert.match(summary,/10 GB/);assert.match(month,/42 GB/);assert.match(month,/另有 2 GB 已计入范围合计/)
  assert.doesNotMatch(month,/44 GB/)
})

test('真实时间图与线路明细将末尾待采样显示为持续更新，真实缺口仍明确提示',async()=>{
  const data=temporal(overview()),t=data.timeseries!
  t.totals.state='partial';t.message_code='sampling_pending'
  data.line_usage={service_id:data.service_id,time_zone:'Asia/Shanghai',period:'7d',range_start:t.range_start,range_end:t.range_end,as_of:t.as_of,summary_revision:'r1',totals:t.totals,boundary_pending_count:0,message_code:'sampling_pending',lines:[{line_id:'line-public',line_name:'本人授权线路',node_name:'节点甲',multiplier:'1',effective_from:'2026-10-01T00:00:00+08:00',effective_to:null,upload_bytes:'12000000000',download_bytes:'18000000000',charged_bytes:'40000000000'}]}
  const pending=(await renderEntry('components/UsageTimeline.vue',{data,period:'7d'},{})).html
  assert.match(pending,/当前时段持续更新，显示最近采样记录。/)
  assert.match(pending,/所选范围已记录/)
  assert.match(pending,/40 GB/)
  assert.doesNotMatch(pending,/部分时段缺少记录|缺失时段未补齐|所选范围合计/)
  t.message_code='partial_coverage';data.line_usage.message_code='partial_coverage'
  const gap=(await renderEntry('components/UsageTimeline.vue',{data,period:'7d'},{})).html
  assert.match(gap,/部分时段缺少记录/)
  assert.match(gap,/缺失时段未补齐/)
  assert.doesNotMatch(gap,/当前时段持续更新/)
})
