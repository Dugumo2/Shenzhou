import test, { afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { ApiError, errorMessage, request, setCsrfToken, setUnauthorizedHandler } from '../src/api.ts'
const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch; setCsrfToken(''); setUnauthorizedHandler(() => {}) })

test('同源写请求使用当前旋转后的CSRF；不保存长期浏览器令牌', async () => {
  let captured: RequestInit | undefined
  globalThis.fetch = async (url, options) => { assert.equal(url, '/api/v1/login'); captured = options; return Response.json({ data: { ok: true }, error: null }) }
  setCsrfToken('old-test-token'); setCsrfToken('rotated-test-token')
  assert.deepEqual(await request('/login', 'POST', { username: 'test' }), { ok: true })
  assert.equal(captured?.credentials, 'same-origin')
  assert.equal(captured?.cache, 'no-store')
  assert.equal((captured?.headers as Record<string, string>)['X-CSRFToken'], 'rotated-test-token')
})
test('空集合与未知量按真实响应保留，不补模拟套餐或零', async () => {
  globalThis.fetch = async () => Response.json({ data: { items: [], used_bytes: null }, error: null })
  assert.deepEqual(await request('/me/services'), { items: [], used_bytes: null })
})
test('401通知会话失效；403、409和字段错误保持真实状态与原因', async () => {
  let expired = 0
  setUnauthorizedHandler(() => { expired++ })
  globalThis.fetch = async () => Response.json({ data: null, error: { code: 'AUTH_REQUIRED', message: '请重新登录。' } }, { status: 401 })
  await assert.rejects(request('/me/services'), (e: unknown) => e instanceof ApiError && e.status === 401)
  assert.equal(expired, 1)
  globalThis.fetch = async () => Response.json({ data: null, error: { code: 'BILLING_CONFLICT', message: '版本已变化。', fields: { next_reset_at: ['请核对时间。'] } } }, { status: 409 })
  await assert.rejects(request('/admin/services/id/billing', 'PATCH', {}), (e: unknown) => e instanceof ApiError && e.status === 409 && errorMessage(e).includes('请核对时间。'))
  assert.equal(expired, 1)
})
test('网络失败和非JSON响应明确失败，没有假成功', async () => {
  globalThis.fetch = async () => { throw new TypeError('network') }
  await assert.rejects(request('/session'), (e: unknown) => e instanceof ApiError && e.code === 'NETWORK_ERROR')
  globalThis.fetch = async () => new Response('<html>error</html>', { status: 503 })
  await assert.rejects(request('/session'), (e: unknown) => e instanceof ApiError && e.status === 503 && e.code === 'INVALID_RESPONSE')
})
