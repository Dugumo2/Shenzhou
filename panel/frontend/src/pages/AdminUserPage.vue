<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { auth } from '../auth'
import { retainServiceResources, resourceReadError } from '../resourceSnapshot'
import { useSnapshotRequest } from '../useSnapshotRequest'
import { useSessionIdentity } from '../useSessionIdentity'
import { formatDate, formatGB } from '../display'
import type { AdminUserDetail } from '../adminTypes'
import type { Service } from '../types'
import BillingDrawer from '../components/BillingDrawer.vue'
import ResourceUsageSummary from '../components/ResourceUsageSummary.vue'
import RefreshControl from '../components/RefreshControl.vue'

const route = useRoute()
const snapshot = useSnapshotRequest<AdminUserDetail>({ reconcile: (next, old) => ({
  ...next, services: next.services.map(service => retainServiceResources(service, old && old.user.id === next.user.id ? old.services.find(item => item.id === service.id) || null : null)),
}) })
const sourceError = computed(() => resourceReadError(snapshot.data.value?.services || []))
const { data: detail, busy, error, initialLoading, lastReadAt } = snapshot
const identity = useSessionIdentity(() => auth.session)
const selectedId = ref<string | null>(null)
const selected = computed(() => {
  const service = detail.value?.services.find(item => item.id === selectedId.value && item.actions?.billing)
  return service ? { ...service, user: { username: detail.value!.user.username } } : null
})
function load() {
  if (!auth.session?.authenticated || !auth.session.user?.is_staff) { snapshot.reset(); return }
  return snapshot.load('/admin/users/' + encodeURIComponent(String(route.params.id)), identity.value)
}
function editBilling(service: Service) {
  if (!service.actions?.billing || !detail.value) return
  selectedId.value = service.id
}
watch(detail, () => { if (!selected.value) selectedId.value = null }, { flush: 'sync' })
watch([() => route.params.id, identity], () => { selectedId.value = null; snapshot.reset(); void load() }, { immediate: true, flush: 'sync' })
</script>
<template>
  <RouterLink :to="{ path: '/admin/users', query: route.query }" class="back-link">← 返回用户管理</RouterLink>
  <div class="page-title"><div><p class="eyebrow">管理员工作区 · 用户详情</p><h1 class="admin-username">{{ detail?.user.username || '用户详情' }}</h1><p class="muted">查看账号与该用户分配的服务。</p></div><RefreshControl :loading="busy" :error="error" :source-error="sourceError" :last-read-at="lastReadAt" @refresh="load" /></div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
  <el-skeleton v-if="initialLoading" :rows="7" animated />
  <template v-if="detail">
    <p v-if="error" class="small muted">本次刷新失败，以下保留上次读取的结果。</p>
    <section class="surface admin-user-summary"><div class="section-title"><h2>账号信息</h2><el-tag :type="detail.user.is_active ? 'success' : 'info'">{{ detail.user.is_active ? '账号已启用' : '账号已停用' }}</el-tag></div>
      <dl class="admin-user-facts"><div><dt>用户编号</dt><dd>#{{ detail.user.id }}</dd></div><div><dt>角色</dt><dd>{{ detail.user.is_staff ? '管理员' : '普通用户' }}</dd></div><div><dt>已分配服务</dt><dd>{{ detail.user.service_count.toLocaleString('zh-CN') }}</dd></div></dl>
      <el-alert v-if="detail.user.mapping_required" title="该用户有旧来源归属待核对。下面仅展示当前已确认归属的服务，未核对的来源不会自动分配。" type="warning" show-icon :closable="false" class="mapping-note" />
    </section>
    <div class="section-title admin-user-services-title"><h2>分配的服务</h2><p class="small muted">每份服务分别查看额度与时间。</p></div>
    <section v-if="!detail.services.length" class="surface empty-state"><div class="empty-symbol" aria-hidden="true">舟</div><h2>当前没有已分配的服务</h2><p>服务开通能力尚未接通，当前仅提供账号与服务查询。</p></section>
    <div v-else class="admin-user-services">
      <section v-for="service in detail.services" :key="service.id" class="surface admin-user-service"><div class="card-heading"><h2>神舟云 <span class="short-id">#{{ service.id.slice(0, 8) }}</span></h2><el-tag :type="service.business_state === 'active' ? 'success' : 'info'">{{ service.status_label }}</el-tag></div>
        <ResourceUsageSummary v-if="service.source_type === 'p8'" :data="service.provider_usage" />
        <dl v-else class="metrics-grid"><div><dt>本期总额度</dt><dd>{{ formatGB(service.quota_bytes) }}</dd></div><div><dt>已用</dt><dd>{{ formatGB(service.used_bytes, '暂无可靠统计') }}</dd><span v-if="service.usage.quality !== 'measured' && service.used_bytes !== null" class="small muted">上次已确认</span></div><div><dt>剩余</dt><dd>{{ formatGB(service.remaining_bytes, '待核算') }}</dd></div><div><dt>下次重置时间</dt><dd class="date-value">{{ formatDate(service.next_reset_at) }}</dd></div><div><dt>到期时间</dt><dd class="date-value">{{ formatDate(service.expires_at) }}</dd></div><div><dt>最后统计时间</dt><dd class="date-value">{{ formatDate(service.usage.updated_at) }}</dd></div></dl>
        <p class="small muted">资料来源：{{ service.source_type === 'p8' ? '原订阅资源' : service.source_type === 'membership' ? '会员登记' : '服务权益' }}</p><p v-if="service.source_type !== 'p8'" class="quality-note">{{ service.usage.message }}</p>
        <div class="admin-service-actions"><el-button v-if="service.actions?.billing" type="primary" plain @click="editBilling(service)">调整下一次重置时间</el-button><span v-else class="small muted">当前服务暂不可调整重置时间。</span></div>
      </section>
    </div>
    <p class="inline-note admin-capability-note">开通、额度、续期、授权与启停操作尚未接通。调整重置时间时会独立预览和保存，不会续期或清空当前用量。</p>
  </template>
  <BillingDrawer :service="selected" @close="selectedId = null" @saved="load" />
</template>
