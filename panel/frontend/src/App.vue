<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { auth, applySession } from './auth'
import { request, errorMessage } from './api'
import { router } from './router'
import type { Session } from './types'
import WorkspaceShell from './layouts/WorkspaceShell.vue'
const route = useRoute()
const adminWorkspace = computed(() => Boolean(auth.session?.user?.is_staff) && (route.path === '/admin' || route.path.startsWith('/admin/')))
const logoutBusy = ref(false), logoutError = ref('')
async function logout() {
  logoutBusy.value = true; logoutError.value = ''
  try { applySession(await request<Session>('/logout', 'POST', {})); await router.replace('/login') }
  catch (e) { logoutError.value = errorMessage(e) }
  finally { logoutBusy.value = false }
}
</script>
<template>
  <div v-if="route.name === 'login'"><RouterView /></div>
  <WorkspaceShell v-else :path="route.path" :admin="adminWorkspace" :username="auth.session?.user?.username" :is-staff="auth.session?.user?.is_staff" :logout-busy="logoutBusy" @logout="logout">
    <template #environment><div v-if="auth.session?.environment?.kind === 'local_candidate'" class="candidate-banner">本地候选{{ auth.session.environment.is_demo ? ' · 演示数据' : '' }}<span>尚未部署生产；真实资产是否导入以核验结果为准。</span></div></template>
    <el-alert v-if="logoutError" :title="logoutError" type="error" show-icon :closable="false" class="spaced" /><RouterView />
  </WorkspaceShell>
</template>
