import test from 'node:test'
import assert from 'node:assert/strict'
import { billingPercent, providerQuality, residentialShare } from '../src/providerUsage.ts'
import type { ProviderMeter } from '../src/providerUsage.ts'
const meter = (): ProviderMeter => ({quality:'provider_reported',updated_at:'2026-10-04T00:00:00Z',
  expires_at:'2026-10-04T07:00:00Z',last_query_ok:true,used_bytes:'50',total_bytes:'200',remaining_bytes:'150'})
test('来源各自时效，刷新页面不能延长采集有效期',()=>{
  assert.equal(providerQuality(meter(),Date.parse('2026-10-04T06:59:00Z')),'provider_reported')
  assert.equal(providerQuality(meter(),Date.parse('2026-10-04T07:00:00Z')),'stale')
  assert.equal(providerQuality({...meter(),quality:'error'},0),'error')
  assert.equal(providerQuality({...meter(),expires_at:null},0),'stale')
})
test('官方账单比例独立计算，超额不画超过100%，未知不补零',()=>{
  assert.equal(billingPercent(meter()),25)
  assert.equal(billingPercent({...meter(),used_bytes:'250',remaining_bytes:'0'}),100)
  assert.equal(billingPercent({...meter(),used_bytes:null}),null)
  assert.equal(billingPercent({...meter(),total_bytes:'0'}),null)
})
test('住宅只展示上传下载构成，不用供应商总额推算住宅余额',()=>{
  assert.equal(residentialShare({...meter(),upload_bytes:'10',download_bytes:'30'}),25)
  assert.equal(residentialShare({...meter(),upload_bytes:null,download_bytes:'30'}),null)
  assert.equal(residentialShare({...meter(),upload_bytes:'0',download_bytes:'0'}),null)
})
