import { ApiError, request } from './api.ts'

export interface SnapshotState<T> {
  key: string | null
  data: T | null
  busy: boolean
  hasLoaded: boolean
  error: unknown | null
  // 浏览器成功读取时刻，与响应内的来源采样时间严格分离。
  lastReadAt: string | null
}

export interface SnapshotRequestOptions<T> {
  read?: (path: string, signal?: AbortSignal) => Promise<T>
  now?: () => string
  timeoutMs?: number
  reconcile?: (next: T, previous: T | null) => T
}

/** 每个实例只持有当前身份和路径的数据，不缓存其他用户或服务。 */
export function createSnapshotRequest<T>(options: SnapshotRequestOptions<T> = {}) {
  const read = options.read ?? ((path: string, signal?: AbortSignal) => request<T>(path, 'GET', undefined, signal))
  const now = options.now ?? (() => new Date().toISOString())
  const timeoutMs = options.timeoutMs ?? 15_000
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new RangeError('读取超时必须为正数。')
  const empty = (): SnapshotState<T> => ({ key: null, data: null, busy: false, hasLoaded: false, error: null, lastReadAt: null })
  let state = empty()
  let generation = 0
  let disposed = false
  let pending: Promise<T | undefined> | null = null
  let active: AbortController | null = null
  const listeners = new Set<(state: SnapshotState<T>) => void>()
  function publish(next: SnapshotState<T>) {
    state = next
    listeners.forEach(listener => listener(state))
  }
  function reset() {
    generation++
    active?.abort()
    active = null
    pending = null
    publish(empty())
  }
  function load(path: string, identity = ''): Promise<T | undefined> {
    if (disposed) return Promise.resolve(undefined)
    const key = JSON.stringify([identity, path])
    if (key === state.key && pending) return pending
    const current = ++generation
    active?.abort()
    const controller = new AbortController()
    active = controller
    const previous = key === state.key ? state : empty()
    let timer: ReturnType<typeof setTimeout>
    let abort: () => void
    // transport即使忽略signal也必须有界结束，取消和超时均参与本地结算。
    const bounded = new Promise<T>((resolve, reject) => {
      abort = () => reject(new ApiError(0, 'REQUEST_ABORTED', '本次读取已取消。'))
      controller.signal.addEventListener('abort', abort, { once: true })
      timer = setTimeout(() => {
        reject(new ApiError(0, 'REQUEST_TIMEOUT', '读取超时，请重试。上次内容已保留。'))
        controller.abort()
      }, timeoutMs)
      Promise.resolve().then(() => {
        if (!controller.signal.aborted) return read(path, controller.signal)
        throw new ApiError(0, 'REQUEST_ABORTED', '本次读取已取消。')
      }).then(resolve, reject)
    })
    // 先登记 Promise，订阅者同步触发第二次读取时也会去重。
    const result = bounded.then(value => {
      if (disposed || current !== generation) return undefined
      const next = options.reconcile ? options.reconcile(value, previous.data) : value
      if (disposed || current !== generation) return undefined
      publish({ ...state, data: next, busy: false, hasLoaded: true, error: null, lastReadAt: now() })
      return next
    }).catch(error => {
      if (disposed || current !== generation) return undefined
      // 明确撤权或对象已删除时立即撤除内容，不能把保旧策略变成越权入口。
      const invalidated = error instanceof ApiError && [401, 403, 404].includes(error.status)
      publish({ ...(invalidated ? { ...empty(), key } : state), busy: false, error })
      return undefined
    }).finally(() => {
      clearTimeout(timer)
      controller.signal.removeEventListener('abort', abort)
      if (current === generation) { pending = null; active = null }
    })
    pending = result
    publish({ ...previous, key, busy: true, error: null })
    return result
  }
  return {
    get state() { return state },
    load,
    reset,
    subscribe(listener: (state: SnapshotState<T>) => void) {
      if (disposed) return () => {}
      listeners.add(listener)
      listener(state)
      return () => { listeners.delete(listener) }
    },
    dispose() {
      disposed = true
      reset()
      listeners.clear()
    },
  }
}
