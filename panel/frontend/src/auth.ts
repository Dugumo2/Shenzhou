import { reactive } from 'vue'
import { request, setCsrfToken } from './api'
import type { Session } from './types'
export const auth = reactive({ session: null as Session | null, ready: false })
let loading: Promise<void> | null = null
export function applySession(session: Session) {
  auth.session = session
  auth.ready = true
  setCsrfToken(session.csrf_token)
}
export async function hydrateSession(force = false) {
  if (auth.ready && !force) return
  if (loading) return loading
  loading = request<Session>('/session').then(applySession).finally(() => { loading = null })
  return loading
}
export function clearSession() { auth.session = null; auth.ready = false; setCsrfToken('') }
