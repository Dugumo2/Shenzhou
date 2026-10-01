<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { applySession, hydrateSession } from '../auth'
import { errorMessage, request } from '../api'
import { router } from '../router'
import type { Session } from '../types'
const route = useRoute()
const username = ref(''), password = ref(''), busy = ref(false), error = ref('')
onMounted(async () => { try { await hydrateSession(true) } catch (e) { error.value = errorMessage(e) } })
async function login() {
  if (!username.value.trim() || !password.value) { error.value = '请输入账号和密码。'; return }
  busy.value = true; error.value = ''
  try {
    await hydrateSession()
    applySession(await request<Session>('/login', 'POST', { username: username.value.trim(), password: password.value }))
    password.value = ''
    const target = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') && !route.query.redirect.startsWith('//') && route.query.redirect !== '/login' ? route.query.redirect : '/services'
    await router.replace(target)
  } catch (e) { error.value = errorMessage(e); password.value = '' }
  finally { busy.value = false }
}
</script>
<template><main class="login-layout">
  <section class="login-intro"><div class="brand light"><span class="brand-symbol">舟</span><span>神舟云</span></div><p class="eyebrow">让连接更简单</p><h1>连接世界，<br>从这里开始。</h1><p>查看服务、了解用量，<br>按你使用的软件轻松设置。</p><div class="intro-note">服务与账号独立管理<br>每一份额度和到期时间都清晰可查</div></section>
  <section class="login-form-panel"><div class="login-form"><p class="eyebrow">欢迎回来</p><h2>登录神舟云</h2><p class="muted">使用你的账号，查看已分配的服务。</p>
    <el-alert v-if="route.query.expired" title="登录已过期，请重新登录。" type="warning" show-icon :closable="false" class="spaced" />
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
    <form @submit.prevent="login"><label for="username">账号</label><el-input id="username" v-model="username" autocomplete="username" placeholder="请输入账号" :disabled="busy" /><label for="password">密码</label><el-input id="password" v-model="password" type="password" show-password autocomplete="current-password" placeholder="请输入密码" :disabled="busy" /><el-button class="login-submit" type="primary" native-type="submit" :loading="busy">登录</el-button></form>
    <p class="register-note">还没有账号？<a href="/register/">使用邀请码注册</a></p>
  </div></section>
</main></template>
