import type { Bytes, Service } from './types.ts'
export function integerBytes(value: Bytes): bigint | null {
  if (value === null) return null
  if (typeof value === 'number') return Number.isSafeInteger(value) && value >= 0 ? BigInt(value) : null
  return /^\d+$/.test(value) ? BigInt(value) : null
}
export function formatGB(value: Bytes, unknown = '暂不可确认'): string {
  const bytes = integerBytes(value)
  if (bytes === null) return unknown
  const hundredths = bytes * 100n / 1_000_000_000n
  const whole = (hundredths / 100n).toLocaleString('zh-CN')
  const fraction = (hundredths % 100n).toString().padStart(2, '0').replace(/0+$/, '')
  return whole + (fraction ? '.' + fraction : '') + ' GB'
}
export function formatDate(value: string | null): string {
  if (!value) return '暂未设置'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '暂不可确认'
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}
export function shanghaiInput(value: string | null): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(date)
  const part = (type: string) => parts.find(p => p.type === type)?.value || ''
  return part('year') + '-' + part('month') + '-' + part('day') + 'T' + part('hour') + ':' + part('minute')
}
export function usagePercent(used: Bytes, quota: Bytes): number | null {
  const u = integerBytes(used), q = integerBytes(quota)
  if (u === null || q === null || q === 0n) return null
  return Math.min(100, Number(u * 100n / q))
}
export function recordedRemaining(quota: Bytes, used: Bytes, remaining: Bytes, applied: boolean): Bytes {
  const q = integerBytes(quota), u = integerBytes(used), r = integerBytes(remaining)
  // 只显示后端提供且与同份已入账金额一致的余额，不从缺失数据猜零。
  if (!applied || q === null || u === null || r === null || r !== (q > u ? q - u : 0n)) return null
  return r.toString()
}
export function serviceUsage(service: Service): { bytes: Bytes; label: string; mode: 'current' | 'last' | 'recorded' | 'booked' | 'estimated' | 'unknown' } {
  if (integerBytes(service.used_bytes) === null && service.usage_estimate?.state === 'estimated' && integerBytes(service.usage_estimate.used_bytes) !== null)
    return { bytes: service.usage_estimate.used_bytes, label: '套餐已用', mode: 'estimated' }
  if (service.remaining_bytes === null && recordedRemaining(service.quota_bytes, service.recorded_used_bytes ?? null,
    service.recorded_remaining_bytes ?? null, service.quota_state === 'applied') !== null)
    return { bytes: service.recorded_used_bytes ?? null, label: '套餐已用', mode: 'booked' }
  if (integerBytes(service.used_bytes) !== null) return { bytes: service.used_bytes,
    label: service.usage.quality === 'measured' ? '本期已用' : '上次已确认用量', mode: service.usage.quality === 'measured' ? 'current' : 'last' }
  if (integerBytes(service.recorded_used_bytes ?? null) !== null) return { bytes: service.recorded_used_bytes ?? null, label: '已记录用量', mode: 'recorded' }
  return { bytes: null, label: '用量（待核算）', mode: 'unknown' }
}
export function serviceRemaining(service: Service): Bytes {
  return service.remaining_bytes ?? (serviceUsage(service).mode === 'estimated' ? service.usage_estimate?.remaining_bytes ?? null :
    recordedRemaining(service.quota_bytes, service.recorded_used_bytes ?? null, service.recorded_remaining_bytes ?? null, service.quota_state === 'applied'))
}
export function shiftLabel(seconds: number): string {
  if (seconds === 0) return '重置时间保持不变'
  const minutes = Math.abs(Math.trunc(seconds / 60))
  const days = Math.floor(minutes / 1440), hours = Math.floor(minutes % 1440 / 60), rest = minutes % 60
  const duration = [days ? days + '天' : '', hours ? hours + '小时' : '', rest ? rest + '分钟' : ''].filter(Boolean).join('') || '0分钟'
  return (seconds > 0 ? '延后' : '提前') + duration
}
export function previewMatches(input: string, previewInput: string, expiresAt: string, now = Date.now()): boolean {
  return input === previewInput && Number.isFinite(Date.parse(expiresAt)) && Date.parse(expiresAt) > now
}
