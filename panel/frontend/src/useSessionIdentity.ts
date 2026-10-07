import { computed, ref, watch } from 'vue'
import type { Session } from './types.ts'

/** 在 setup 中调用；身份只含本地序号，会话令牌仅用于识别轮换。 */
export function useSessionIdentity(getSession: () => Session | null) {
  const epoch = ref(0)
  watch([
    getSession,
    () => getSession()?.user?.username,
    () => getSession()?.user?.is_staff,
    () => getSession()?.authenticated,
    () => getSession()?.csrf_token,
  ], () => { epoch.value++ }, { flush: 'sync' })
  // watch 自动随所属 setup/effectScope 销毁，值中不携带用户名、令牌或 Cookie。
  return computed(() => 'session:' + epoch.value)
}
