<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { auth, applySession } from './auth'
import { request, errorMessage } from './api'
import { router } from './router'
import type { Session } from './types'
const route = useRoute()
const adminWorkspace = computed(() => route.path === '/admin' || route.path.startsWith('/admin/'))
const adminNavigation = [
  { path: '/admin/overview', label: '管理总览', description: '用户与服务状态' },
  { path: '/admin/users', label: '用户管理', description: '查找账号与分配的服务' },
  { path: '/admin/services', label: '订阅管理', description: '额度、到期与重置时间' },
  { path: '/admin/rules', label: '代理规则', description: '编辑候选与检查匹配' },
]
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
  <div v-else class="application" :class="{ 'admin-application': adminWorkspace }">
    <header v-if="adminWorkspace" class="site-header admin-header"><div class="header-inner">
      <RouterLink to="/admin/overview" class="brand" aria-label="神舟云管理总览"><span class="brand-symbol">舟</span><span>神舟云<small>管理员工作区</small></span></RouterLink>
      <span class="admin-workspace-label">管理面板</span>
      <div class="user-area"><span class="username" :title="auth.session?.user?.username">{{ auth.session?.user?.username }}</span><el-button text :loading="logoutBusy" @click="logout">退出</el-button></div>
    </div></header>
    <header v-else class="site-header"><div class="header-inner">
      <RouterLink to="/services" class="brand" aria-label="神舟云首页"><span class="brand-symbol">舟</span><span>神舟云<small>让连接更简单</small></span></RouterLink>
      <nav aria-label="主导航">
        <RouterLink to="/services" :class="{ 'router-link-active': route.path.startsWith('/services') }">我的服务</RouterLink>
        <RouterLink to="/guides">使用指南</RouterLink><RouterLink to="/account">账号设置</RouterLink>
      </nav>
      <div class="user-area"><RouterLink v-if="auth.session?.user?.is_staff" to="/admin/overview" class="admin-entry">管理面板</RouterLink><span class="username" :title="auth.session?.user?.username">{{ auth.session?.user?.username }}</span><el-button text :loading="logoutBusy" @click="logout">退出</el-button></div>
    </div></header>
    <div v-if="auth.session?.environment?.kind === 'local_candidate'" class="candidate-banner">本地候选{{ auth.session.environment.is_demo ? ' · 演示数据' : '' }}<span>尚未部署生产；真实资产是否导入以核验结果为准。</span></div>
    <div v-if="adminWorkspace" class="admin-workspace">
      <aside class="admin-sidebar surface">
        <p class="eyebrow">管理工作区</p>
        <nav aria-label="管理导航"><RouterLink v-for="item in adminNavigation" :key="item.path" :to="item.path" :class="{ 'admin-nav-active': route.path === item.path || route.path.startsWith(item.path + '/') }"><strong>{{ item.label }}</strong><span>{{ item.description }}</span></RouterLink></nav>
        <RouterLink to="/services" class="workspace-return">← 切回我的服务</RouterLink>
      </aside>
      <main class="admin-main"><el-alert v-if="logoutError" :title="logoutError" type="error" show-icon :closable="false" class="spaced" /><RouterView /></main>
    </div>
    <main v-else class="page-container"><el-alert v-if="logoutError" :title="logoutError" type="error" show-icon :closable="false" class="spaced" /><RouterView /></main>
    <footer class="site-footer">神舟云 · 每一份服务，清楚可查</footer>
  </div>
</template>
