import { shallowReactive } from 'vue'
import { ApiError } from './api.ts'

export interface SaveBody {
  next_reset_at: string
  expected_billing_revision: number
  preview_token: string
  idempotency_key: string
}
// 只保存在本页内存，跨组件卸载保留；不持久化凭证到浏览器存储。
const pending = shallowReactive(new Map<string, SaveBody>())
const running = new Map<string, Promise<unknown>>()
export function pendingKey(username: string, serviceId: string): string {
  return JSON.stringify([username, serviceId])
}
export function getPending(key: string): SaveBody | null { return pending.get(key) || null }
export function rememberPending(key: string, body: SaveBody): SaveBody {
  const existing = pending.get(key)
  if (existing) return existing
  pending.set(key, Object.freeze({ ...body }))
  return pending.get(key)!
}
export function definitiveRejection(error: unknown): boolean {
  return error instanceof ApiError && (
    error.status === 422 && error.code === 'INVALID_INPUT' ||
    error.status === 409 && ['BILLING_CONFLICT', 'PREVIEW_INVALID'].includes(error.code)
  )
}
export async function boundedRequest<T>(operation: Promise<T>, timeoutMs = 15_000): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([operation, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new ApiError(0, 'REQUEST_TIMEOUT', '请求等待超时，结果尚未确认。')), timeoutMs)
    })])
  } finally { clearTimeout(timer) }
}
export function submitPending<T>(key: string, body: SaveBody, send: () => Promise<T>): Promise<T> {
  const attemptKey = JSON.stringify([key, body.idempotency_key])
  const active = running.get(attemptKey)
  if (active) return active as Promise<T>
  const clearMatching = () => {
    if (pending.get(key)?.idempotency_key === body.idempotency_key) pending.delete(key)
  }
  const attempt = Promise.resolve().then(send).then(result => {
    clearMatching()
    return result
  }, error => {
    if (definitiveRejection(error)) clearMatching()
    throw error
  }).finally(() => { if (running.get(attemptKey) === attempt) running.delete(attemptKey) })
  running.set(attemptKey, attempt)
  return attempt
}
