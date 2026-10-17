import { reactive } from 'vue'
import type { Router } from 'vue-router'

export type RecoveryStatus = 'idle' | 'checking' | 'update' | 'retry'
export type RecoveryState = { status: RecoveryStatus; message: string; target: string; dirty: boolean; updating: boolean }
type RecoveryOptions = {
  version: string
  fetchVersion: (signal: AbortSignal) => Promise<string>
  navigate: (target: string) => Promise<unknown>
  reload: (target: string) => void
  confirm: (message: string) => boolean
  timeoutMs?: number
}

/** 只接受应用内路由；原始值和解码值同时校验，避免外站/反斜杠绕过。 */
export function safeRecoveryTarget(target: unknown): string {
  if (typeof target !== 'string' || target.length > 2048) return '/services'
  try {
    const decoded = decodeURIComponent(target)
    if (![target, decoded].every(value => value.startsWith('/') && !value.startsWith('//')
      && !/[\\\u0000-\u001f\u007f]/.test(value))) return '/services'
  } catch { return '/services' }
  return target
}

export function createReleaseRecovery(options: RecoveryOptions, state: RecoveryState = reactive({
  status: 'idle', message: '', target: '/services', dirty: false, updating: false,
})) {
  let checking: Promise<void> | undefined
  let generation = 0
  const check = (target: string) => {
    state.target = safeRecoveryTarget(target)
    if (checking) return checking
    const current = ++generation
    state.status = 'checking'; state.message = '页面未能打开，正在检查可用版本。'
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => { controller.abort(); reject(new Error('version_timeout')) }, options.timeoutMs ?? 5000)
    })
    checking = (async () => {
      try {
        const latest = await Promise.race([options.fetchVersion(controller.signal), timeout])
        if (current !== generation) return
        if (/^[a-f0-9]{64}$/.test(options.version) && /^[a-f0-9]{64}$/.test(latest) && latest !== options.version) {
          state.status = 'update'; state.message = '页面已有新版本。更新后将继续前往目标页面，登录会话将保留。'
        } else {
          state.status = 'retry'; state.message = '页面暂时未能加载，未发现可用的新版本。请检查网络后重试打开。'
        }
      } catch {
        if (current === generation) {
          state.status = 'retry'; state.message = '暂时无法检查页面版本。当前内容已保留，请检查网络后重试。'
        }
      } finally { if (timer) clearTimeout(timer); checking = undefined }
    })()
    return checking
  }
  const reloadDocument = () => {
    if (state.updating) return
    if (state.dirty && !options.confirm('当前页面可能有未保存的输入。请先取消并保存或复制需要保留的内容；确定更新会丢弃这些输入。')) return
    state.updating = true
    options.reload(safeRecoveryTarget(state.target))
  }
  const update = () => { if (state.status === 'update') reloadDocument() }
  // 同版本的模块加载失败也可能被浏览器缓存，只有明确操作才重建文档。
  const reload = () => { if (state.status === 'retry') reloadDocument() }
  const retry = async () => {
    if (checking || state.updating) return
    if (state.dirty && !options.confirm('当前页面可能有未保存的输入。请先取消并保存或复制需要保留的内容；确定继续会离开当前页面。')) return
    state.status = 'checking'; state.message = '正在重新打开目标页面。'
    try {
      const failure = await options.navigate(state.target)
      if (failure) { await check(state.target); return }
      recovered()
    } catch { await check(state.target) }
  }
  const recovered = () => {
    generation++; state.status = 'idle'; state.message = ''; state.dirty = false
  }
  return { state, check, update, reload, retry, recovered, markDirty: () => { state.dirty = true } }
}

export const releaseRecovery = reactive<RecoveryState>({ status: 'idle', message: '', target: '/services', dirty: false, updating: false })
let installed: ReturnType<typeof createReleaseRecovery> | undefined
export function updateFrontendRelease() { installed?.update() }
export function reloadFrontendPage() { installed?.reload() }
export function retryReleaseRecovery() { return installed?.retry() }

/** 不接触登录POST、Cookie或输入值；恢复只进行GET、应用路由和用户确认的整页更新。 */
export function installReleaseRecovery(router: Router) {
  const version = document.querySelector<HTMLMetaElement>('meta[name="frontend-release"]')?.content ?? ''
  installed = createReleaseRecovery({ version,
    fetchVersion: async signal => {
      const response = await fetch('/app/release-version.json', { method: 'GET', cache: 'no-store', credentials: 'same-origin', signal })
      if (!response.ok) throw new Error('version_unavailable')
      const data = await response.json()
      if (data?.schema_version !== 1 || typeof data.version !== 'string') throw new Error('invalid_version')
      return data.version
    },
    navigate: target => router.replace(target),
    reload: target => {
      // 只改hash不会重取HTML；先固定安全目标，再显式加载新入口。
      window.history.replaceState(null, '', `/app/#${safeRecoveryTarget(target)}`)
      window.location.reload()
    },
    confirm: message => window.confirm(message),
  }, releaseRecovery)
  const recovery = installed
  router.onError((_error, to) => { void recovery.check(to.fullPath) })
  router.afterEach((_to, _from, failure) => { if (!failure) recovery.recovered() })
  window.addEventListener('vite:preloadError', () => {
    // 不preventDefault：保留import拒绝，让登录页和路由完成自己的错误处理。
    void recovery.check(router.currentRoute.value.fullPath)
  })
  for (const name of ['input', 'change']) document.addEventListener(name, event => {
    if (event.target instanceof Element && event.target.closest('input, textarea, select, [contenteditable="true"]')) recovery.markDirty()
  }, true)
}
