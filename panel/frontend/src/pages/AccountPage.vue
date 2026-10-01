<script setup lang="ts">
import { auth } from '../auth'
</script>
<template><div class="page-title"><div><p class="eyebrow">账号与安全</p><h1>账号设置</h1><p class="muted">妥善保管密码，保护你的服务。</p></div></div><section class="surface account-panel"><h2>{{ auth.session?.user?.username }}</h2><p class="muted">{{ auth.session?.user?.is_staff ? '管理员账号' : '普通账号' }}</p><div class="account-section"><h3>修改密码</h3><p>修改密码时需验证当前密码，并由服务器检查新密码强度。</p><ul><li v-for="message in auth.session?.password_policy.messages" :key="message">{{ message }}</li></ul><a class="primary-link" href="/account/">修改密码 →</a><p class="small muted">打开账号安全页面，完成后可返回我的服务。</p></div></section></template>
