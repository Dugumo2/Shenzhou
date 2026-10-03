import test from 'node:test'
import assert from 'node:assert/strict'
import { quotaGauge, transferShare, historyBars } from '../src/usageCharts.ts'
import type { UsageOverviewData } from '../src/usage-types.ts'

function sample(): UsageOverviewData {
  return { service_id:'test-only', source_type:'entitlement', time_zone:'Asia/Shanghai', generated_at:'2026-10-03T01:00:00Z',period:'current',
    current_cycle:{starts_at:'2026-09-30T16:00:00Z',ends_at:'2026-11-01T00:00:00Z'},
    summary:{quota_bytes:'100',quota_state:'applied',charged_bytes:'30',remaining_bytes:'70',upload_bytes:'10',download_bytes:'20',next_reset_at:null},
    quality:{state:'measured',message:'',collected_at:null},
    history:{kind:'confirmed_ledger_postings',date_basis:'created_at',range_start:'2026-09-30T16:00:00Z',range_end:'2026-11-01T00:00:00Z',totals:null,
      record_count:2,excluded_record_count:0,day_limit:90,days_truncated:false,message:'',days:[
        {date:'2026-10-01',record_count:1,charged_bytes:'30',upload_bytes:'10',download_bytes:'20'},
        {date:'2026-10-03',record_count:1,charged_bytes:'0',upload_bytes:'0',download_bytes:'0'},
      ]},
  }
}
test('环图区分当前可靠比例、上次确认与未知，不自行计算剩余',()=>{
  const d=sample(); assert.deepEqual(quotaGauge(d),{mode:'current',percent:30})
  d.quality.state='stale';d.summary.remaining_bytes=null
  assert.deepEqual(quotaGauge(d),{mode:'recorded',percent:30});assert.equal(d.summary.remaining_bytes,null)
  for(const quality of ['unknown','gap'] as const){d.quality.state=quality;assert.equal(quotaGauge(d).percent,null)}
  d.quality.state='stale';d.summary.quota_state='configured';assert.equal(quotaGauge(d).percent,null)
})
test('上海日期记录图明确区分缺失与零，不延伸到未来账期或均分样本',()=>{
  const d=sample(), bars=historyBars(d,'charged')
  assert.deepEqual(bars.map(p=>[p.date,p.value]),[['2026-10-01','30'],['2026-10-02',null],['2026-10-03','0']])
  assert.equal(bars[0].height,100);assert.equal(bars[1].records,0)
  const copy=JSON.stringify(d); historyBars(d,'transfer');assert.equal(JSON.stringify(d),copy)
})
test('原始传输量与倍率折算分别读取，缺一个方向不能补零',()=>{
  const d=sample();d.history.days[0].charged_bytes='90'
  assert.equal(historyBars(d,'transfer')[0].value,'30')
  assert.equal(historyBars(d,'charged')[0].value,'90')
  d.history.days[0].upload_bytes=null;assert.equal(historyBars(d,'transfer')[0].value,null)
  assert.equal(transferShare(d)?.upPercent,33.33)
  d.summary.download_bytes=null;assert.equal(transferShare(d),null)
})
test('超过安全整数仍正确缩放，零总额与缺周期不伪造图表',()=>{
  const d=sample();d.summary.quota_bytes='18014398509481986000';d.summary.charged_bytes='9007199254740993000';d.summary.remaining_bytes='9007199254740993000'
  assert.equal(quotaGauge(d).percent,50)
  d.summary.upload_bytes='0';d.summary.download_bytes='0';assert.equal(transferShare(d)?.empty,true)
  d.summary.quota_bytes='0';assert.equal(quotaGauge(d).percent,null)
  d.history.range_start=null;assert.deepEqual(historyBars(d,'charged'),[])
})
test('长期账期最多显示90天，非法记录日期不会成为图点',()=>{
  const d=sample();d.history.range_start='2020-01-01T00:00:00Z';d.history.days.push({...d.history.days[0],date:'2026-02-31'})
  const bars=historyBars(d,'charged');assert.equal(bars.length,90);assert.equal(bars.at(-1)?.date,'2026-10-03')
  assert.ok(bars.every(b=>b.date!=='2026-02-31'))
})
