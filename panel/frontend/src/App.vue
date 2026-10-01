<script setup lang="ts">
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { auth, applySession } from './auth'
import { request, errorMessage } from './api'
import { router } from './router'
import type { Session } from './types'
const route = useRoute()
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
  <div v-else class="application">
    <header class="site-header"><div class="header-inner">
      <RouterLink to="/services" class="brand" aria-label="神舟云首页"><span class="brand-symbol">舟</span><span>神舟云<small>让连接更简单</small></span></RouterLink>
      <nav aria-label="主导航">
        <RouterLink to="/services" :class="{ 'router-link-active': route.path.startsWith('/services') }">我的服务</RouterLink>
        <RouterLink to="/guides">使用指南</RouterLink><RouterLink to="/account">账号设置</RouterLink>
        <RouterLink v-if="auth.session?.user?.is_staff" to="/admin/services">订阅管理</RouterLink>
        <RouterLink v-if="auth.session?.user?.is_staff" to="/admin/rules">代理规则</RouterLink>
      </nav>
      <div class="user-area"><span>{{ auth.session?.user?.username }}</span><el-button text :loading="logoutBusy" @click="logout">退出</el-button></div>
    </div></header>
    <div v-if="auth.session?.environment?.kind === 'local_candidate'" class="candidate-banner">本地候选{{ auth.session.environment.is_demo ? ' · 演示数据' : '' }}<span>尚未部署生产；真实资产是否导入以核验结果为准。</span></div>
    <main class="page-container"><el-alert v-if="logoutError" :title="logoutError" type="error" show-icon :closable="false" class="spaced" /><RouterView /></main>
    <footer class="site-footer">神舟云 · 每一份服务，清楚可查</footer>
  </div>
</template>
