<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { auth, applySession } from './auth'
import { request, errorMessage } from './api'
import { router } from './router'
import type { Session } from './types'
import WorkspaceShell from './layouts/WorkspaceShell.vue'
import { releaseRecovery, updateFrontendRelease, retryReleaseRecovery, reloadFrontendPage } from './releaseRecovery'
const route = useRoute()
const adminWorkspace = computed(() => Boolean(auth.session?.user?.is_staff) && (route.path === '/admin' || route.path.startsWith('/admin/')))
const logoutBusy = ref(false), logoutError = ref('')
const recoveryBanner = ref<HTMLElement | null>(null)
let recoveryObserver: ResizeObserver | undefined
watch(recoveryBanner, element => {
  recoveryObserver?.disconnect()
  if (typeof document === 'undefined') return
  const resize = () => document.documentElement.style.setProperty('--release-recovery-height', `${element?.getBoundingClientRect().height ?? 0}px`)
  resize()
  if (element && typeof ResizeObserver !== 'undefined') {
    recoveryObserver = new ResizeObserver(resize)
    recoveryObserver.observe(element)
  }
}, { flush: 'post' })
onBeforeUnmount(() => {
  recoveryObserver?.disconnect()
  if (typeof document !== 'undefined') document.documentElement.style.removeProperty('--release-recovery-height')
})
async function logout() {
  logoutBusy.value = true; logoutError.value = ''
  try { applySession(await request<Session>('/logout', 'POST', {})); await router.replace('/login') }
  catch (e) { logoutError.value = errorMessage(e) }
  finally { logoutBusy.value = false }
}
</script>
<template>
  <section v-if="releaseRecovery.status !== 'idle'" ref="recoveryBanner" class="release-recovery" role="status" aria-live="polite" aria-label="页面更新与恢复">
    <p>{{ releaseRecovery.message }}</p>
    <el-button v-if="releaseRecovery.status === 'update'" type="primary" :disabled="releaseRecovery.updating" @click="updateFrontendRelease">更新并继续</el-button>
    <div v-else-if="releaseRecovery.status === 'retry'" class="recovery-actions">
      <el-button :disabled="releaseRecovery.updating" @click="retryReleaseRecovery">重试打开</el-button>
      <el-button type="primary" :disabled="releaseRecovery.updating" @click="reloadFrontendPage">重新载入页面</el-button>
    </div>
  </section>
  <div v-if="route.name === 'login'"><RouterView /></div>
  <WorkspaceShell v-else :path="route.path" :admin="adminWorkspace" :username="auth.session?.user?.username" :is-staff="auth.session?.user?.is_staff" :logout-busy="logoutBusy" @logout="logout">
    <template #environment><div v-if="auth.session?.environment?.kind === 'local_candidate'" class="candidate-banner">本地候选{{ auth.session.environment.is_demo ? ' · 演示数据' : '' }}<span>尚未部署生产；真实资产是否导入以核验结果为准。</span></div></template>
    <el-alert v-if="logoutError" :title="logoutError" type="error" show-icon :closable="false" class="spaced" /><RouterView />
  </WorkspaceShell>
</template>
<style scoped>
.release-recovery{position:sticky;top:0;z-index:2100;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 24px;background:#fff8e8;border-bottom:1px solid #ecd8a1;color:#51401f}.release-recovery p{margin:0;line-height:1.6}.release-recovery .el-button{flex-shrink:0}.recovery-actions{display:flex;flex-shrink:0;gap:8px}.recovery-actions .el-button+.el-button{margin-left:0}:global(body:has(.release-recovery) .el-overlay){top:var(--release-recovery-height,0px);height:calc(100% - var(--release-recovery-height,0px))}@media(max-width:640px){.release-recovery{align-items:flex-start;flex-direction:column;padding:12px 16px;gap:10px}}
</style>
