import { reactive } from 'vue'
import { ApiError, request, setCsrfToken } from './api'
import type { Session } from './types'
export const auth = reactive({ session: null as Session | null, ready: false })
let loading: { promise: Promise<void>; controller: AbortController } | null = null
let generation = 0
let loginController: AbortController | null = null
function invalidateRequests() {
  generation++
  loading?.controller.abort()
  loading = null
  loginController?.abort()
  loginController = null
}
export function applySession(session: Session) {
  invalidateRequests()
  auth.session = session
  auth.ready = true
  setCsrfToken(session.csrf_token)
}
export async function hydrateSession(force = false) {
  if (auth.ready && !force) return
  if (loading) return loading.promise
  const version = generation, controller = new AbortController()
  const current = { controller, promise: Promise.resolve() }
  current.promise = request<Session>('/session', 'GET', undefined, controller.signal).then(session => {
    if (generation === version && !controller.signal.aborted) applySession(session)
  }).catch(error => {
    if (generation === version && !controller.signal.aborted) throw error
  }).finally(() => { if (loading === current) loading = null })
  loading = current
  return current.promise
}
export function clearSession() { invalidateRequests(); auth.session = null; auth.ready = false; setCsrfToken('') }

export function isSessionFor(username: string, session = auth.session): boolean {
  // 与服务端 normalize_username 一致；不能将其他已登录账号借给当前输入。
  return session?.authenticated === true && session.user?.username === username.trim().normalize('NFKC')
}
function accountConflict() {
  return new ApiError(409, 'different_authenticated_account', '当前已登录其他账号，请先退出当前账号后再登录。')
}
function requireCurrent(version: number, controller: AbortController) {
  if (generation !== version || controller.signal.aborted)
    throw new ApiError(0, 'REQUEST_ABORTED', '登录状态已变化，请确认当前账号后重试。')
}
export async function authenticate(username: string, password: string): Promise<void> {
  if (loginController) throw new ApiError(409, 'login_in_progress', '正在登录，请稍候。')
  // 每次提交先只读确认现有会话，响应丢失后也不会盲目重复 POST。
  const preflightVersion = generation
  await hydrateSession(true)
  if (generation !== preflightVersion + 1 || !auth.ready)
    throw new ApiError(0, 'REQUEST_ABORTED', '登录状态已变化，请确认当前账号后重试。')
  if (auth.session?.authenticated) {
    if (!isSessionFor(username)) throw accountConflict()
    return
  }
  if (!password) throw new ApiError(401, 'session_expired', '登录已过期，请重新输入账号和密码。')
  if (loginController) throw new ApiError(409, 'login_in_progress', '正在登录，请稍候。')
  const controller = new AbortController(), version = generation
  loginController = controller
  try {
    let session: Session
    try {
      session = await request<Session>('/login', 'POST', { username: username.trim(), password }, controller.signal)
      requireCurrent(version, controller)
      if (!session || session.authenticated !== true || !session.user || typeof session.csrf_token !== 'string')
        throw new ApiError(200, 'INVALID_RESPONSE', '暂时无法确认登录状态，请稍后再试。')
    } catch (error) {
      const ambiguous = error instanceof ApiError && (error.code === 'already_authenticated'
        || error.code === 'NETWORK_ERROR' || error.code === 'INVALID_RESPONSE' || error.status >= 500)
      if (!ambiguous) throw error
      requireCurrent(version, controller)
      // 仅恢复同账号的真实会话，不重发密码，也不把认证失败当成功。
      try { session = await request<Session>('/session', 'GET', undefined, controller.signal) }
      catch { throw new ApiError(0, 'session_recovery_failed', '暂时无法确认登录状态，请稍后再试。') }
      requireCurrent(version, controller)
      if (!session?.authenticated) {
        if (session?.authenticated === false && typeof session.csrf_token === 'string') applySession(session)
        throw new ApiError(0, 'session_not_established', '尚未确认登录成功，请重新登录。')
      }
    }
    requireCurrent(version, controller)
    if (!isSessionFor(username, session)) throw accountConflict()
    if (typeof session.csrf_token !== 'string') throw new ApiError(200, 'INVALID_RESPONSE', '登录状态无效，请稍后再试。')
    applySession(session)
  } finally { if (loginController === controller) loginController = null }
}
