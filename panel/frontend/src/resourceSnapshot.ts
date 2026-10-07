import type { ProviderUsage } from './providerUsage.ts'

type WithUsage = { provider_usage?: ProviderUsage }

/** 仅保留同一个已获准来源的旧测量；不沿用权限或来源发生变化的数据。 */
export function retainResourceUsage<T extends WithUsage>(next: T, previous: WithUsage | null): T {
  const incoming = next.provider_usage, old = previous?.provider_usage
  if (!incoming?.error || incoming.meters.length || !incoming.source_key ||
      incoming.source_key !== old?.source_key || !old.meters.length) return next
  return { ...next, provider_usage: {
    ...incoming,
    meters: old.meters.map(meter => ({ ...meter, quality: 'error', alerts: [
      ...meter.alerts.filter(alert => alert.code !== 'snapshot_read_failed'),
      { code: 'snapshot_read_failed', severity: 'error', message: incoming.error!.message },
    ] })),
  } }
}

export function retainServiceResources<T extends WithUsage & { id: string }>(next: T, previous: (WithUsage & { id: string }) | null): T {
  return retainResourceUsage(next, previous?.id === next.id ? previous : null)
}

export function retainResourceList<T extends { items: (WithUsage & { id: string })[] }>(next: T, previous: T | null): T {
  const old = new Map(previous?.items.map(item => [item.id, item]))
  return { ...next, items: next.items.map(item => retainServiceResources(item, old.get(item.id) || null)) }
}

/** 页面读取成功也可能携带来源失败；读取反馈不能掩盖来源状态。 */
export function resourceReadError(items: WithUsage[]): string {
  return items.find(item => item.provider_usage?.error)?.provider_usage?.error?.message || ''
}
