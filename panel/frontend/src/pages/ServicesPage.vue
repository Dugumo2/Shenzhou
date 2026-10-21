<script setup lang="ts">
import { computed, watch } from 'vue'
import { formatDate, formatGB, serviceRemaining, serviceUsage, usagePercent } from '../display'
import type { Compatibility, Service } from '../types'
import { auth } from '../auth'
import { retainResourceList } from '../resourceSnapshot'
import { useSessionIdentity } from '../useSessionIdentity'
import { useSnapshotRequest } from '../useSnapshotRequest'
import RefreshControl from '../components/RefreshControl.vue'
const identity = useSessionIdentity(() => auth.session)
const snapshot = useSnapshotRequest<{ items: Service[]; compatibility: Compatibility }>({ reconcile: retainResourceList })
const services = computed(() => snapshot.data.value?.items || [])
const compatibility = computed(() => snapshot.data.value?.compatibility || null)
const { busy, error, lastReadAt, initialLoading } = snapshot
function load() { return snapshot.load('/me/services', identity.value) }
watch(identity, () => { snapshot.reset(); if (auth.session?.authenticated) void load() }, { immediate: true, flush: 'sync' })
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">我的空间</p><h1>我的服务</h1><p class="muted">额度、到期和使用方法，一目了然。</p></div><RefreshControl :loading="busy" :error="error" :last-read-at="lastReadAt" @refresh="load" /></div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
  <el-alert v-if="compatibility?.message" :title="compatibility.message" type="warning" show-icon :closable="false" class="spaced" />
  <el-skeleton v-if="initialLoading" :rows="5" animated />
  <div v-else-if="!error && !services.length" class="surface empty-state"><div class="empty-symbol">舟</div><h2>暂时还没有服务</h2><p>账号已准备好。请联系管理员为你分配服务。</p><RouterLink to="/guides">先看看使用指南 →</RouterLink></div>
  <div v-else class="service-grid"><article v-for="(service, index) in services" :key="service.id" class="surface service-card">
    <div class="card-heading"><div><p class="eyebrow">服务 {{ String(index + 1).padStart(2, '0') }}</p><h2>{{ service.name || '神舟云' }}</h2></div><el-tag :type="['active', 'applied'].includes(service.service_status?.state || service.business_state || '') ? 'success' : 'info'" effect="light">{{ service.service_status?.label || service.status_label }}</el-tag></div>
    <p v-if="service.quota_state === 'configured'" class="small muted">套餐额度待应用</p>
    <div class="usage-number"><span v-if="['estimated', 'booked'].includes(serviceUsage(service).mode)">套餐已用 </span><span v-else-if="serviceUsage(service).mode === 'recorded'">已记录 </span>{{ formatGB(serviceUsage(service).bytes, '暂无可靠统计') }}<span> / {{ formatGB(service.quota_bytes) }}</span></div>
    <el-progress v-if="service.usage.quality === 'measured' && service.quota_state === 'applied' && service.remaining_bytes !== null && usagePercent(service.used_bytes, service.quota_bytes) !== null" :percentage="usagePercent(service.used_bytes, service.quota_bytes) || 0" :show-text="false" :stroke-width="7" />
    <p v-if="service.usage.quality === 'stale'" class="muted small" role="status">用量更新延迟，请稍后更新。</p>
    <p v-else-if="serviceUsage(service).mode === 'unknown'" class="muted small" role="status">用量暂不可用，请稍后更新。</p>
    <dl class="card-facts"><div><dt>剩余额度</dt><dd>{{ formatGB(serviceRemaining(service), '待核算') }}</dd></div><div><dt>到期时间</dt><dd>{{ formatDate(service.expires_at) }}</dd></div><div><dt>下次流量重置</dt><dd>{{ formatDate(service.next_reset_at) }}</dd></div></dl>
    <details class="service-information"><summary>服务资料</summary><dl><div><dt>服务编号</dt><dd>{{ service.id }}</dd></div></dl></details>
    <RouterLink :to="'/services/' + service.id" class="card-link">查看服务与使用方法 <span>→</span></RouterLink>
  </article></div>
</template>
<style scoped>
.service-card { min-width: 0; }
.card-heading { gap: 12px; flex-wrap: wrap; }
.card-heading > div { min-width: 0; }
.card-heading h2, .card-facts dd, .usage-number { overflow-wrap: anywhere; }
.service-information { margin: 16px 0; font-size: 13px; }
.service-information summary { width: fit-content; cursor: pointer; color: #55657e; }
.service-information dl { margin: 12px 0 0; }
.service-information dt { color: #78869b; }
.service-information dd { margin: 5px 0 0; overflow-wrap: anywhere; }
@media (max-width: 420px) {
  .card-facts > div { min-width: 0; }
}
</style>
