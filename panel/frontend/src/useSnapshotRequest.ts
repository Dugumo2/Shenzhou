import { computed, onScopeDispose, shallowRef } from 'vue'
import { ApiError, errorMessage } from './api.ts'
import { createSnapshotRequest } from './snapshotRequest.ts'
import type { SnapshotRequestOptions } from './snapshotRequest.ts'

/** 在 setup 中创建；账号/会话/权限变化需更新 load 的身份参数或立即 reset。 */
export function useSnapshotRequest<T>(options: SnapshotRequestOptions<T> = {}) {
  const controller = createSnapshotRequest<T>(options)
  const state = shallowRef(controller.state)
  const unsubscribe = controller.subscribe(value => { state.value = value })
  onScopeDispose(() => { controller.dispose(); unsubscribe() })
  return {
    data: computed(() => state.value.data),
    busy: computed(() => state.value.busy),
    initialLoading: computed(() => state.value.busy && !state.value.hasLoaded),
    refreshing: computed(() => state.value.busy && state.value.hasLoaded),
    hasLoaded: computed(() => state.value.hasLoaded),
    error: computed(() => state.value.error === null ? '' : errorMessage(state.value.error)),
    accessDenied: computed(() => state.value.error instanceof ApiError && [401, 403, 404].includes(state.value.error.status)),
    lastReadAt: computed(() => state.value.lastReadAt),
    load: controller.load,
    reset: controller.reset,
  }
}
