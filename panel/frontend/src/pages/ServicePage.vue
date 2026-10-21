<script setup lang="ts">
import { computed, ref, shallowRef, watch, onScopeDispose } from 'vue'
import { useRoute } from 'vue-router'
import { useSnapshotRequest } from '../useSnapshotRequest'
import { usageRangeCache, usageNaturalRange } from '../usageRangeCache'
import { usageNavigation } from '../usageNavigation'
import { usageResponseIsCurrent } from '../usageTemporal'
import { ApiError } from '../api'
import { retainServiceResources, retainResourceUsage } from '../resourceSnapshot'
import type { UsageOverviewData, CalendarPeriod } from '../usage-types'
import RefreshControl from '../components/RefreshControl.vue'
import type { Client, Service } from '../types'
import ServiceMetrics from '../components/ServiceMetrics.vue'
import UsageOverview from '../components/UsageOverview.vue'
import DeliveryResources from '../components/DeliveryResources.vue'
import P8DeliveryResources from '../components/P8DeliveryResources.vue'
import { auth, sessionScope } from '../auth'
const route = useRoute()
const identity = computed(() => 'usage-session:' + sessionScope.generation)
const serviceSnapshot = useSnapshotRequest<Service>({ reconcile: retainServiceResources })
let rejectedBoundary = false, boundaryRetryUsed = false
const usageSnapshot = useSnapshotRequest<UsageOverviewData>({ reconcile: (next, previous) => {
  if (!usageResponseIsCurrent(next)) {
    rejectedBoundary = true
    throw new ApiError(0, 'USAGE_BOUNDARY_CHANGED', '用量周期已变化，请更新数据。')
  }
  return retainResourceUsage(next, previous)
} })
const catalogSnapshot = useSnapshotRequest<{ items: Client[] }>()
const service = serviceSnapshot.data
const canonicalId = ref<string | null>(null)
const latestUsage = shallowRef<UsageOverviewData | null>(null)
// 用量接口初次失败仍可展示已核实的套餐资料；不从采购资源推算任何字段。
const summarySnapshot = computed<UsageOverviewData | null>(() => {
  if (latestUsage.value) return latestUsage.value
  const value = service.value
  if (!value || usageSnapshot.accessDenied.value) return null
  return { service_id: value.id, source_type: value.source_type, time_zone: 'Asia/Shanghai', generated_at: value.usage.updated_at || new Date().toISOString(), period: '7d', current_cycle: null,
    summary: { usage_estimate: value.usage_estimate ?? null, quota_bytes: value.quota_bytes?.toString() ?? null, quota_state: value.quota_state || 'unknown', charged_bytes: value.used_bytes?.toString() ?? null,
      recorded_used_bytes: value.recorded_used_bytes?.toString() ?? null, recorded_upload_bytes: value.recorded_upload_bytes?.toString() ?? null, recorded_download_bytes: value.recorded_download_bytes?.toString() ?? null,
      recorded_remaining_bytes: value.recorded_remaining_bytes?.toString() ?? null,
      remaining_bytes: value.remaining_bytes?.toString() ?? null, upload_bytes: value.recorded_upload_bytes?.toString() ?? null, download_bytes: value.recorded_download_bytes?.toString() ?? null, next_reset_at: value.next_reset_at },
    quality: { state: 'unknown', message: '', collected_at: value.usage.updated_at },
    history: { kind:'confirmed_ledger_postings', date_basis:'created_at', range_start:null, range_end:null, totals:null, record_count:0, excluded_record_count:0, days:[], day_limit:90, days_truncated:false, message:'' } }
})
function clearServiceCache(forgetSelection = true) {
  const id = service.value?.id || canonicalId.value || String(route.params.id)
  usageRangeCache.clearService(identity.value, id)
  if (forgetSelection) usageNavigation.clearService(identity.value, id)
  latestUsage.value = null
}
watch(usageSnapshot.accessDenied, denied => { if (denied) { clearServiceCache(); serviceSnapshot.reset() } }, { flush:'sync' })
watch(serviceSnapshot.accessDenied, denied => { if (denied) { clearServiceCache(); usageSnapshot.reset() } }, { flush:'sync' })
const clients = computed(() => catalogSnapshot.data.value?.items || [])
const busy = computed(() => serviceSnapshot.busy.value || usageSnapshot.busy.value || catalogSnapshot.busy.value)
const boundaryError = ref('')
const error = computed(() => serviceSnapshot.error.value || catalogSnapshot.error.value || boundaryError.value)
const updateError = computed(() => error.value || usageSnapshot.error.value)
const lastReadAt = ref<string | null>(null)
const device = ref(''), os = ref(''), clientId = ref('')
const usagePeriod = ref<CalendarPeriod>('7d')
let restoring = false
let boundaryTimer: ReturnType<typeof setTimeout> | undefined
const refreshKey = ref(0)
const activeSection = ref('overview'), choosing = ref(true)
const deviceOptions = [{ id: 'phone', label: '手机', hint: 'Android / iOS' }, { id: 'computer', label: '电脑', hint: 'Windows / macOS / Linux' }, { id: 'router', label: '路由器', hint: '查看适配情况' }]
const osOptions = computed(() => device.value === 'phone' ? ['Android', 'iOS'] : device.value === 'computer' ? ['Windows', 'macOS', 'Linux'] : [])
const matched = computed(() => clients.value.filter(c => c.device === device.value && (device.value === 'router' || c.os === os.value)))
const selected = computed(() => matched.value.find(c => c.id === clientId.value))
const delivery = computed(() => service.value?.clients?.find(c => c.id === clientId.value)?.delivery || service.value?.delivery)
// 只有后端明确返回下载地址时才提供下载动作；缺少地址时保持未交付状态，避免把提示文案当成成功。
const downloadUrl = computed(() => {
  const value = delivery.value?.download_url
  return typeof value === 'string' && value.trim() ? value : null
})
watch(device, () => { os.value = ''; clientId.value = '' })
watch(os, () => { clientId.value = '' })
watch(clientId, value => { choosing.value = !value })
async function loadUsage() {
  if (!service.value) return
  const owner = identity.value, id = service.value.id, period = usagePeriod.value
  const key = { identityGeneration: owner, serviceId: id, period }
  const path = '/me/services/' + encodeURIComponent(id) + '/usage?period=' + period
  const cached = usageRangeCache.get(key)
  if (cached) usageSnapshot.prime(path, owner, cached.snapshot, cached.lastReadAt)
  // 范围图可以复用旧结果，套餐概览始终采用同服务较新的摘要。
  for (const p of ['7d', 'month', 'year'] as CalendarPeriod[]) {
    const entry = usageRangeCache.get({ ...key, period: p })
    if (entry) acceptSummary(entry.snapshot)
  }
  const value = await usageSnapshot.load(path, owner)
  if (owner === identity.value && id === service.value?.id && rejectedBoundary) {
    rejectedBoundary = false
    clearServiceCache(false); usageSnapshot.reset(); serviceSnapshot.reset()
    if (!boundaryRetryUsed) {
      boundaryRetryUsed = true
      clearTimeout(boundaryTimer); boundaryTimer = setTimeout(() => { void load() }, 1000)
    } else boundaryError.value = '用量周期正在更新，请稍后点击更新数据。'
    return
  }
  if (!value || owner !== identity.value || id !== service.value?.id || period !== usagePeriod.value) return
  if (value.service_id !== id || value.period !== period) { usageSnapshot.reset(); return }
  usageRangeCache.set(key, value, usageSnapshot.lastReadAt.value || new Date().toISOString())
  acceptSummary(value)
  boundaryRetryUsed = false
  boundaryError.value = ''
  scheduleBoundary()
  return value
}
function acceptSummary(value: UsageOverviewData) {
  if (!latestUsage.value || Date.parse(value.generated_at) >= Date.parse(latestUsage.value.generated_at)) latestUsage.value = value
}
function scheduleBoundary() {
  clearTimeout(boundaryTimer)
  if (!service.value) return
  const now = Date.now()
  const boundaries = [usageNaturalRange(usagePeriod.value, now).dayEnd,
    Date.parse(latestUsage.value?.summary.next_reset_at || service.value.next_reset_at || ''),
    Date.parse(latestUsage.value?.current_cycle?.ends_at || '')].filter(value => Number.isFinite(value) && value > now)
  boundaryTimer = setTimeout(() => {
    clearServiceCache(false); usageSnapshot.reset(); serviceSnapshot.reset()
    void load()
  }, Math.min(...boundaries) - now + 25)
}
function revalidateVisible() {
  if (document.visibilityState !== 'visible' || !service.value) return
  const old = latestUsage.value
  if (old && (usageNaturalRange(usagePeriod.value, Date.now()).dayStart > Date.parse(old.generated_at)
    || (old.summary.next_reset_at && Date.parse(old.summary.next_reset_at) <= Date.now()))) {
    clearServiceCache(false); usageSnapshot.reset(); serviceSnapshot.reset(); void load()
  }
}
if (typeof document !== 'undefined') document.addEventListener('visibilitychange', revalidateVisible)
onScopeDispose(() => { clearTimeout(boundaryTimer); if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', revalidateVisible) })
async function load() {
  const owner = identity.value
  // 刷新下载权限时，交付组件自行重新核验；统计与软件选择保留。
  refreshKey.value++
  const [detail] = await Promise.all([
    serviceSnapshot.load('/me/services/' + encodeURIComponent(String(route.params.id)), owner),
    catalogSnapshot.hasLoaded.value ? Promise.resolve() : catalogSnapshot.load('/catalog/clients', owner),
  ])
  if (owner !== identity.value) return
  if (!detail) {
    if (!service.value) { usageSnapshot.reset(); lastReadAt.value = null }
    return
  }
  // 别名映射后只按正式服务编号读取用量，概览与用量分区共享这一份结果。
  canonicalId.value = detail.id
  restoring = true
  usagePeriod.value = usageNavigation.get(owner, detail.id)
  restoring = false
  scheduleBoundary()
  const usage = await loadUsage()
  if (usage && owner === identity.value && !catalogSnapshot.error.value) lastReadAt.value = new Date().toISOString()
}
watch(usagePeriod, value => {
  if (service.value && !restoring) { usageNavigation.set(identity.value, service.value.id, value); void loadUsage() }
}, { flush: 'sync' })
watch(() => [route.params.id, identity.value], () => {
  clearTimeout(boundaryTimer)
  rejectedBoundary = false; boundaryRetryUsed = false; boundaryError.value = ''
  serviceSnapshot.reset(); usageSnapshot.reset(); catalogSnapshot.reset(); latestUsage.value = null; canonicalId.value = null; lastReadAt.value = null
  device.value = ''; os.value = ''; clientId.value = ''; activeSection.value = 'overview'; usagePeriod.value = '7d'
  if (auth.session?.authenticated) void load()
}, { immediate: true, flush: 'sync' })
</script>
<template>
  <RouterLink to="/services" class="back-link">← 我的服务</RouterLink>
  <div class="page-title"><div><p class="eyebrow">服务详情</p><h1>{{ service?.name || '神舟云' }}</h1></div><RefreshControl :loading="busy" :error="updateError" :last-read-at="lastReadAt" @refresh="load" /></div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
  <el-skeleton v-if="busy && !service" :rows="6" animated />
  <template v-if="service">
    <el-tabs v-model="activeSection" class="service-sections" aria-label="服务功能分区">
    <el-tab-pane label="服务概览" name="overview">
    <UsageOverview :key="service.id + '-overview'" :service-id="service.id" :refresh-key="refreshKey" managed :snapshot="summarySnapshot" :loading="usageSnapshot.busy.value" :load-error="usageSnapshot.error.value" summary-only />
    <section class="surface detail-summary"><ServiceMetrics :service="service" lifecycle-only /></section>
    <div class="service-actions"><button class="surface" @click="activeSection = 'connect'"><strong>连接设置 →</strong><span>选择设备和软件，按步骤导入订阅。</span></button><button class="surface" @click="activeSection = 'usage'"><strong>查看流量用量 →</strong><span>按近7天、本月或今年查看套餐消耗。</span></button></div>
    </el-tab-pane>
    <el-tab-pane label="流量用量" name="usage" lazy>
    <UsageOverview :key="service.id" :service-id="service.id" :refresh-key="refreshKey" managed :selected-period="usagePeriod" :snapshot="usageSnapshot.data.value" :loading="usageSnapshot.busy.value" :load-error="usageSnapshot.error.value" @period-change="usagePeriod = $event" />
    </el-tab-pane>
    <el-tab-pane label="连接设置" name="connect" lazy>
    <section class="surface setup-panel"><div class="section-title"><div><h2>{{ selected && !choosing ? selected.name + ' 连接设置' : '选择设备和软件' }}</h2><p class="muted small">更换软件沿用这份服务，无需重新选择线路。</p></div><el-button v-if="selected && !choosing" @click="choosing = true">更换设备或软件</el-button></div>
      <template v-if="choosing || !selected">
      <h3><span class="step-number">1</span>你准备在哪种设备上使用？</h3>
      <div class="device-options"><button v-for="option in deviceOptions" :key="option.id" type="button" :class="{ selected: device === option.id }" :aria-pressed="device === option.id" @click="device = option.id"><strong>{{ option.label }}</strong><span>{{ option.hint }}</span></button></div>
      <template v-if="device && device !== 'router'"><h3><span class="step-number">2</span>选择系统</h3><el-radio-group v-model="os" aria-label="系统"><el-radio-button v-for="system in osOptions" :key="system" :value="system">{{ system }}</el-radio-button></el-radio-group></template>
      <template v-if="device === 'router'"><el-alert title="路由器暂未提供经过验证的适配。" description="请选择电脑或手机查看现有软件指南。这里不会生成未经验证的链接。" type="info" :closable="false" show-icon class="spaced" /></template>
      <template v-else-if="os"><h3><span class="step-number">3</span>选择软件</h3><el-radio-group v-if="matched.length" v-model="clientId" aria-label="软件"><el-radio-button v-for="client in matched" :key="client.id" :value="client.id">{{ client.name }}</el-radio-button></el-radio-group><p v-else class="inline-note">这个系统暂未提供经过验证的适配。请选择其他系统查看现有软件指南。</p></template>
      <el-button v-if="selected" class="spaced" @click="choosing = false">继续使用 {{ selected.name }}</el-button>
      </template>
      <div v-if="selected" v-show="!choosing" class="software-result">
        <P8DeliveryResources v-if="service.source_type === 'p8'" :key="service.id + ':' + selected.id" :service-id="service.id" :client-id="selected.id" :refresh-key="refreshKey" />
        <DeliveryResources v-else-if="auth.session?.environment?.is_demo" :key="service.id + ':' + selected.id" :service-id="service.id" :client-id="selected.id" />
        <template v-else-if="downloadUrl">
          <el-button tag="a" :href="downloadUrl" target="_blank" rel="noopener">下载资源</el-button>
          <p class="small muted">下载完成后仍需在软件中导入并应用；下载成功不代表客户端已生效。</p>
        </template>
        <el-alert v-else :title="'资源尚未提供可用下载链接。' + (delivery?.message ? ' ' + delivery.message : '')" type="info" show-icon :closable="false" />
        <RouterLink :to="{ path: '/guides', query: { client: selected.id } }" class="text-action">查看 {{ selected.name }} 使用指南 →</RouterLink>
      </div>
    </section>
    </el-tab-pane>
    </el-tabs>
  </template>
</template>
<style scoped>
.service-sections { margin-top: 12px; }
.service-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.service-actions button { padding: 24px; text-align: left; border: 1px solid var(--border, #e0e7ef); cursor: pointer; font: inherit; color: inherit; }
.service-actions strong, .service-actions span { display: block; }
.service-actions strong { color: var(--el-color-primary); font-size: 18px; margin-bottom: 9px; }
.service-actions span { color: var(--muted, #687887); font-size: 14px; }
.service-actions button:hover { border-color: var(--el-color-primary); }
@media(max-width: 640px) {
  .service-actions { grid-template-columns: 1fr; }
  .page-title { flex-direction: column; align-items: flex-start; gap: 14px; }
  .page-title h1 { white-space: nowrap; }
}
</style>
