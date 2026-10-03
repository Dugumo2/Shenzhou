<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { errorMessage, request } from '../api'
import { formatDate, formatGB } from '../display'
import type { AdminUserDetail } from '../adminTypes'
import type { Service } from '../types'
import BillingDrawer from '../components/BillingDrawer.vue'

const route = useRoute()
const detail = ref<AdminUserDetail | null>(null), selected = ref<Service | null>(null)
const busy = ref(true), error = ref('')
let generation = 0
async function load() {
  const current = ++generation
  busy.value = true; error.value = ''
  try { const data = await request<AdminUserDetail>('/admin/users/' + encodeURIComponent(String(route.params.id))); if (current === generation) detail.value = data }
  catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}
function editBilling(service: Service) {
  if (!service.actions?.billing || !detail.value) return
  selected.value = { ...service, user: { username: detail.value.user.username } }
}
watch(() => route.params.id, () => { detail.value = null; selected.value = null; void load() }, { immediate: true })
onUnmounted(() => { generation++ })
</script>
<template>
  <RouterLink :to="{ path: '/admin/users', query: route.query }" class="back-link">← 返回用户管理</RouterLink>
  <div class="page-title"><div><p class="eyebrow">管理员工作区 · 用户详情</p><h1 class="admin-username">{{ detail?.user.username || '用户详情' }}</h1><p class="muted">查看账号与该用户分配的服务。</p></div><el-button :loading="busy" @click="load">刷新</el-button></div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
  <el-skeleton v-if="busy && !detail" :rows="7" animated />
  <template v-if="detail">
    <p v-if="error" class="small muted">本次刷新失败，以下保留上次读取的结果。</p>
    <section class="surface admin-user-summary"><div class="section-title"><h2>账号信息</h2><el-tag :type="detail.user.is_active ? 'success' : 'info'">{{ detail.user.is_active ? '账号已启用' : '账号已停用' }}</el-tag></div>
      <dl class="admin-user-facts"><div><dt>用户编号</dt><dd>#{{ detail.user.id }}</dd></div><div><dt>角色</dt><dd>{{ detail.user.is_staff ? '管理员' : '普通用户' }}</dd></div><div><dt>已分配服务</dt><dd>{{ detail.user.service_count.toLocaleString('zh-CN') }}</dd></div></dl>
      <el-alert v-if="detail.user.mapping_required" title="该用户有旧来源归属待核对。下面仅展示当前已确认归属的服务，未核对的来源不会自动分配。" type="warning" show-icon :closable="false" class="mapping-note" />
    </section>
    <div class="section-title admin-user-services-title"><h2>分配的服务</h2><p class="small muted">每份服务分别查看额度与时间。</p></div>
    <section v-if="!detail.services.length" class="surface empty-state"><div class="empty-symbol" aria-hidden="true">舟</div><h2>当前没有已分配的服务</h2><p>服务开通能力尚未接通，当前仅提供账号与服务查询。</p></section>
    <div v-else class="admin-user-services">
      <section v-for="service in detail.services" :key="service.id" class="surface admin-user-service"><div class="card-heading"><h2>神舟云 <span class="short-id">#{{ service.id.slice(0, 8) }}</span></h2><el-tag :type="service.state === 'active' ? 'success' : 'info'">{{ service.status_label }}</el-tag></div>
        <dl class="metrics-grid"><div><dt>本期总额度</dt><dd>{{ formatGB(service.quota_bytes) }}</dd></div><div><dt>已用</dt><dd>{{ formatGB(service.used_bytes, '暂无可靠统计') }}</dd><span v-if="service.usage.quality !== 'measured' && service.used_bytes !== null" class="small muted">上次已确认</span></div><div><dt>剩余</dt><dd>{{ formatGB(service.remaining_bytes, '待核算') }}</dd></div><div><dt>下次重置时间</dt><dd class="date-value">{{ formatDate(service.next_reset_at) }}</dd></div><div><dt>到期时间</dt><dd class="date-value">{{ formatDate(service.expires_at) }}</dd></div><div><dt>最后统计时间</dt><dd class="date-value">{{ formatDate(service.usage.updated_at) }}</dd></div></dl>
        <p class="quality-note">{{ service.usage.message }}</p>
        <div class="admin-service-actions"><el-button v-if="service.actions?.billing" type="primary" plain @click="editBilling(service)">调整下一次重置时间</el-button><span v-else class="small muted">当前服务暂不可调整重置时间。</span></div>
      </section>
    </div>
    <p class="inline-note admin-capability-note">开通、额度、续期、授权与启停操作尚未接通。调整重置时间时会独立预览和保存，不会续期或清空当前用量。</p>
  </template>
  <BillingDrawer :service="selected" @close="selected = null" @saved="load" />
</template>
