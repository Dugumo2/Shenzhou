<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { auth } from '../auth'
import { formatDate } from '../display'
import type { AdminOverview } from '../adminTypes'
import { useSessionIdentity } from '../useSessionIdentity'
import { useSnapshotRequest } from '../useSnapshotRequest'
import AdminResourceUsage from '../components/AdminResourceUsage.vue'
import RefreshControl from '../components/RefreshControl.vue'

const identity = useSessionIdentity(() => auth.session)
const snapshot = useSnapshotRequest<AdminOverview>()
const overview = snapshot.data
const resourceUsage = ref<InstanceType<typeof AdminResourceUsage> | null>(null)
const busy = computed(() => snapshot.busy.value || Boolean(resourceUsage.value?.busy))
const error = computed(() => snapshot.error.value || resourceUsage.value?.error || '')
const sourceError = computed(() => resourceUsage.value?.sourceError || '')
const lastReadAt = computed(() => {
  const dates = [snapshot.lastReadAt.value, resourceUsage.value?.lastReadAt].filter((value): value is string => Boolean(value))
  return dates.sort()[0] || null
})
function knownCount(value: number | null): value is number { return value !== null && Number.isSafeInteger(value) && value >= 0 }
function count(value: number | null): string { return knownCount(value) ? value.toLocaleString('zh-CN') : '—' }
function loadOverview() {
  if (!auth.session?.authenticated || !auth.session.user?.is_staff) { snapshot.reset(); return }
  return snapshot.load('/admin/overview', identity.value)
}
async function load() { await Promise.all([loadOverview(), resourceUsage.value?.load()]) }
watch(identity, () => { snapshot.reset(); void loadOverview() }, { immediate: true, flush: 'sync' })
// 一处明确撤权后同时撤除本页其他管理员数据；暂时断网仍保留旧内容。
watch(snapshot.accessDenied, denied => { if (denied) resourceUsage.value?.reset() }, { flush: 'sync' })
watch(() => resourceUsage.value?.accessDenied, denied => { if (denied) snapshot.reset() }, { flush: 'sync' })
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员工作区</p><h1>管理总览</h1><p class="muted">管理用户与订阅，查看资源用量。</p></div><RefreshControl :loading="busy" :error="error" :source-error="sourceError" :last-read-at="lastReadAt" @refresh="load" /></div>
  <section class="surface personal-usage-entry" aria-label="本人套餐流量入口"><div><h2>我的套餐与流量</h2><p class="muted">查看本人套餐余额及近7天、本月、今年的流量趋势。</p></div><RouterLink to="/services" class="personal-usage-link">进入我的服务 <span aria-hidden="true">→</span></RouterLink></section>
  <el-alert v-if="snapshot.error.value" :title="snapshot.error.value" type="error" show-icon :closable="false" class="spaced" />
  <el-skeleton v-if="snapshot.initialLoading.value" :rows="6" animated />
  <template v-if="overview">
    <section class="surface overview-section"><div class="section-title"><h2>用户</h2><RouterLink to="/admin/users" class="small">进入用户管理 →</RouterLink></div>
      <dl class="overview-metrics"><div><dt>用户总数</dt><dd>{{ count(overview.users.total) }}</dd></div><div><dt>已启用账号</dt><dd>{{ count(overview.users.active) }}</dd></div><div><dt>已停用账号</dt><dd>{{ count(overview.users.disabled) }}</dd></div></dl>
    </section>
    <section class="surface overview-section"><div class="section-title"><h2>服务与核对事项</h2><RouterLink to="/admin/services" class="small">进入订阅管理 →</RouterLink></div>
      <dl class="overview-metrics"><div><dt>服务总数</dt><dd>{{ count(overview.services.total) }}</dd></div><div><dt>已到期服务</dt><dd>{{ count(overview.services.expired) }}</dd></div><div><dt>待核对用户</dt><dd>{{ count(overview.services.mapping_required) }}</dd></div><div><dt>已记录计量缺口</dt><dd>{{ count(overview.services.recorded_metering_gaps) }}</dd></div><div v-if="knownCount(overview.services.statistics_incomplete)"><dt>待核对计量</dt><dd>{{ count(overview.services.statistics_incomplete) }}</dd></div></dl>
    </section>
    <p class="small muted overview-updated">汇总时间：{{ formatDate(overview.generated_at) }}<span v-if="overview.environment?.is_demo"> · 演示数据</span></p>
  </template>
  <AdminResourceUsage ref="resourceUsage" controlled />
</template>
<style scoped>
.personal-usage-entry{display:flex;align-items:center;justify-content:space-between;gap:24px;padding:24px 26px;margin-bottom:22px}.personal-usage-entry h2{margin:0 0 8px;font-size:19px}.personal-usage-entry p{margin:0;line-height:1.7}.personal-usage-link{display:inline-flex;gap:14px;align-items:center;flex-shrink:0;padding:12px 16px;border-radius:9px;background:#edf3ff;color:var(--el-color-primary,#365ccb);font-weight:600;text-decoration:none}.personal-usage-link:focus-visible{outline:2px solid currentColor;outline-offset:3px}@media(max-width:640px){.personal-usage-entry{padding:18px;align-items:flex-start;flex-direction:column;gap:14px}.personal-usage-link{max-width:100%}.section-title{gap:12px;flex-wrap:wrap}}
</style>
