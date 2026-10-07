import test from 'node:test'
import assert from 'node:assert/strict'
import { retainServiceResources, retainResourceUsage, retainResourceList, resourceReadError } from '../src/resourceSnapshot.ts'
import { createSnapshotRequest } from '../src/snapshotRequest.ts'
import type { ProviderUsage } from '../src/providerUsage.ts'

function source(): ProviderUsage {
  return { schema_version: 2, source_key: 'same-authorized-source', generated_at: '2026-10-07T01:00:00Z', meters: [{
    id: 'resource', label: '独立资源', scope: 'external_node', source_kind: 'estimate', quality: 'current', quota_bytes: '400000000000',
    used_bytes: '73000000000', remaining_bytes: '327000000000', upload_bytes: '23000000000', download_bytes: '50000000000',
    observed_at: '2026-10-07T00:59:00Z', expires_at: '2026-10-07T01:05:00Z', expires_on: '2027-01-19',
    cycle: { kind: 'fixed_days', starts_at: null, ends_at: null, next_reset_at: '2026-10-20T00:00:00+08:00' }, alerts: [], details: {},
  }] }
}
function failed(key: string | undefined = 'same-authorized-source'): ProviderUsage {
  return { schema_version: 2, source_key: key, generated_at: null, meters: [], error: { code: 'usage_unavailable', message: '统计源暂不可更新' } }
}

test('HTTP200来源错误保留同来源已知图形数据和采样日期，明确标错且不修改输入', () => {
  const old = { id: 'service-a', provider_usage: source() }, original = structuredClone(old)
  const next = { id: 'service-a', provider_usage: failed() }
  const merged = retainServiceResources(next, old)
  assert.equal(merged.provider_usage.meters[0].used_bytes, '73000000000')
  assert.equal(merged.provider_usage.meters[0].observed_at, old.provider_usage.meters[0].observed_at)
  assert.equal(merged.provider_usage.meters[0].quality, 'error')
  assert.equal(merged.provider_usage.generated_at, null)
  assert.equal(resourceReadError([merged]), '统计源暂不可更新')
  assert.deepEqual(old, original)
  assert.equal(next.provider_usage.meters.length, 0)
  assert.equal(retainServiceResources(next, merged).provider_usage.meters[0].alerts.length, 1)
})

test('权限或来源变更、其他服务、首次错误没有旧图；缺少来源指纹不得猜归属', () => {
  const old = { id: 'service-a', provider_usage: source() }
  for (const key of ['new-source', undefined]) {
    const next = { id: 'service-a', provider_usage: { ...failed(), source_key: key } }
    assert.equal(retainServiceResources(next, old).provider_usage.meters.length, 0)
  }
  assert.equal(retainServiceResources({ id: 'service-b', provider_usage: failed() }, old).provider_usage.meters.length, 0)
  assert.equal(retainServiceResources({ id: 'service-a', provider_usage: failed() }, null).provider_usage.meters.length, 0)
  assert.deepEqual(retainServiceResources({ id: 'service-a' }, old), { id: 'service-a' })
  const recovered = { id: 'service-a', provider_usage: source() }
  assert.equal(retainServiceResources(recovered, { id: 'service-a', provider_usage: failed() }), recovered)
})

test('列表只合并相同服务，不恢复已删除服务或旧权限', () => {
  const old = { items: [{ id: 'a', provider_usage: source() }, { id: 'b', provider_usage: source() }] }
  const result = retainResourceList({ items: [{ id: 'a', provider_usage: failed() }, { id: 'c' }] }, old)
  assert.deepEqual(result.items.map(item => item.id), ['a', 'c'])
  assert.equal(result.items[0].provider_usage?.meters.length, 1)
  assert.equal(result.items[1].provider_usage, undefined)
})

test('请求控制器实际经过正常→200来源失败→恢复，保留图形但不假报来源成功', async () => {
  const inputs = [{ provider_usage: source() }, { provider_usage: failed() }, { provider_usage: source() }]
  const reader = createSnapshotRequest({ read: async () => inputs.shift()!, reconcile: retainResourceUsage })
  await reader.load('/usage', 'owner')
  const sampleTime = reader.state.data?.provider_usage.meters[0].observed_at
  await reader.load('/usage', 'owner')
  assert.equal(reader.state.data?.provider_usage.meters.length, 1)
  assert.equal(reader.state.data?.provider_usage.meters[0].observed_at, sampleTime)
  assert.ok(resourceReadError([reader.state.data!]))
  await reader.load('/usage', 'owner')
  assert.equal(resourceReadError([reader.state.data!]), '')
  assert.equal(reader.state.data?.provider_usage.meters[0].quality, 'current')
  reader.dispose()
})
