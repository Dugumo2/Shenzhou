export class ApiError extends Error {
  status: number
  code: string
  fields: Record<string, string[]>
  constructor(status: number, code: string, message: string, fields: Record<string, string[]> = {}) {
    super(message); this.name = 'ApiError'; this.status = status; this.code = code; this.fields = fields
  }
}
let csrfToken = ''
let onUnauthorized: (() => void) | null = null
export function setCsrfToken(token: string) { csrfToken = token }
export function setUnauthorizedHandler(handler: () => void) { onUnauthorized = handler }
export async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (method !== 'GET') { headers['Content-Type'] = 'application/json'; headers['X-CSRFToken'] = csrfToken }
  let response: Response
  try {
    response = await fetch('/api/v1' + path, { method, headers, credentials: 'same-origin', cache: 'no-store', ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
  } catch { throw new ApiError(0, 'NETWORK_ERROR', '暂时无法连接，请检查网络后重试。当前内容已保留。') }
  let payload: { data?: T; error?: { code?: string; message?: string; fields?: Record<string, string[]> } }
  try { payload = await response.json() } catch {
    if (response.status === 401) onUnauthorized?.()
    throw new ApiError(response.status, 'INVALID_RESPONSE', '服务暂时无法返回有效内容，请稍后重试。')
  }
  if (!response.ok || payload.error) {
    if (response.status === 401) onUnauthorized?.()
    throw new ApiError(response.status, payload.error?.code || 'REQUEST_FAILED', payload.error?.message || '操作未完成，请稍后重试。', payload.error?.fields || {})
  }
  if (!('data' in payload)) throw new ApiError(response.status, 'INVALID_RESPONSE', '服务返回的内容不完整，请稍后重试。')
  return payload.data as T
}
export function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return '暂时无法读取，请稍后重试。'
  const fields = Object.values(error.fields).flat().join('；')
  return fields ? error.message + ' ' + fields : error.message
}
