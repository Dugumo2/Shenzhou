import { onUnmounted, ref } from 'vue'

export function effectiveFreshness(state: string, expiresAt: string | undefined, now = Date.now()): string {
  if (state !== 'fresh') return state
  const expires = expiresAt ? Date.parse(expiresAt) : NaN
  if (!Number.isFinite(expires)) return 'unknown'
  return expires <= now ? 'stale' : 'fresh'
}

export function useInventoryClock() {
  const now = ref(Date.now())
  // 只更新已有证据的时效标签，不轮询网络或触发采集。
  const timer = setInterval(() => { now.value = Date.now() }, 1000)
  onUnmounted(() => clearInterval(timer))
  return now
}
