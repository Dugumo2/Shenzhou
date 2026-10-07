<script setup lang="ts">
import { computed, watch } from 'vue'
import { auth } from '../auth'
import { useSessionIdentity } from '../useSessionIdentity'
import { useSnapshotRequest } from '../useSnapshotRequest'
import { retainResourceUsage, resourceReadError } from '../resourceSnapshot'
import type { ProviderUsage } from '../providerUsage'
import ProviderUsageDashboard from './ProviderUsageDashboard.vue'
import RefreshControl from './RefreshControl.vue'
type ResourceItem = { service_id: string; provider_usage: ProviderUsage }
const identity = useSessionIdentity(() => auth.session)
const snapshot = useSnapshotRequest<{ items: ResourceItem[] }>({ reconcile: (next, previous) => ({ ...next, items: next.items.map(item => retainResourceUsage(item, previous?.items.find(old => old.service_id === item.service_id) || null)) }) })
const { busy, error, initialLoading, lastReadAt } = snapshot
const items = computed(() => snapshot.data.value?.items || [])
const sourceError = computed(() => resourceReadError(items.value))
function load() {
  if (!auth.session?.authenticated || !auth.session.user?.is_staff) { snapshot.reset(); return }
  return snapshot.load('/admin/resource-usage', identity.value)
}
watch(identity, () => { snapshot.reset(); void load() }, { immediate:true, flush:'sync' })
</script>
<template>
  <section class="surface resource-section" aria-label="资源采购用量">
    <div class="section-title"><div><h2>资源采购用量</h2><p class="small muted">外部节点与服务器各自的容量预算和采样记录。</p></div><RefreshControl :loading="busy" :error="error" :source-error="sourceError" :last-read-at="lastReadAt" @refresh="load" /></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-skeleton v-if="initialLoading" :rows="4" animated />
    <ProviderUsageDashboard v-for="item in items" :key="item.service_id" :data="item.provider_usage" />
    <p v-if="!busy && !error && !items.length" class="small muted">当前没有可读取的资源采购统计。</p>
  </section>
</template>
<style scoped>.resource-section { padding:26px; margin:22px 0; }@media(max-width:640px) { .resource-section { padding:18px; } }</style>
