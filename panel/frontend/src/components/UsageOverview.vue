<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useSnapshotRequest } from '../useSnapshotRequest'
import { useSessionIdentity } from '../useSessionIdentity'
import { auth } from '../auth'
import RefreshControl from './RefreshControl.vue'
import { formatDate } from '../display'
import UsageDashboard from './UsageDashboard.vue'
import UsageTimeline from './UsageTimeline.vue'
import type { UsageOverviewData, UsagePeriod, CalendarPeriod } from '../usage-types'
const props = defineProps<{
  serviceId: string; refreshKey?: number; summaryOnly?: boolean
  managed?: boolean; snapshot?: UsageOverviewData | null; loading?: boolean; loadError?: string
}>()
const emit = defineEmits<{ 'period-change': [period: UsagePeriod] }>()
const identity = useSessionIdentity(() => auth.session)
const reader = useSnapshotRequest<UsageOverviewData>()
const period = ref<CalendarPeriod>('7d')
const data = computed(() => props.managed ? props.snapshot || null : reader.data.value)
const busy = computed(() => props.managed ? !!props.loading : reader.busy.value)
const error = computed(() => props.managed ? props.loadError || '' : reader.error.value)
const headingId = computed(() => 'usage-' + props.serviceId + (props.summaryOnly ? '-summary' : '-detail'))
const qualityLabel = computed(() => ({ measured: '当前用量已确认', stale: '显示上次记录', gap: '部分时段缺少记录', unknown: '尚未取得用量记录' })[data.value?.quality.state || 'unknown'])
const periods: { id: CalendarPeriod; label: string }[] = [{ id: '7d', label: '近7天' }, { id: 'month', label: '本月' }, { id: 'year', label: '今年' }]
function load() {
  if (props.managed) return
  return reader.load('/me/services/' + encodeURIComponent(props.serviceId) + '/usage?period=' + period.value, identity.value)
}
watch(() => [props.serviceId, props.refreshKey, period.value, identity.value], () => { if (!props.managed) void load() }, { immediate: true })
watch(period, value => { if (props.managed) emit('period-change', value) })
</script>
<template>
  <section class="surface usage-overview" :aria-labelledby="headingId" :aria-busy="busy">
    <div class="section-title"><div><h2 :id="headingId">{{ summaryOnly ? '套餐流量概览' : '流量用量' }}</h2><p class="small muted">{{ summaryOnly ? '查看本期套餐额度与原始上传、下载构成。' : '按实际采样时间查看这份套餐的使用情况。' }}</p></div><RefreshControl v-if="!managed" :loading="busy" :error="error" :last-read-at="reader.lastReadAt.value" @refresh="load" /></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <template v-if="summaryOnly">
      <el-skeleton v-if="busy && !data" :rows="4" animated />
      <template v-if="data">
        <div class="quality-line"><el-tag :type="data.quality.state === 'measured' ? 'success' : 'warning'">{{ qualityLabel }}</el-tag><span class="small muted">最后采集：{{ data.quality.collected_at ? formatDate(data.quality.collected_at) : '暂无采集记录' }}</span></div>
        <UsageDashboard :data="data" compact />
        <dl class="usage-dates"><div><dt>本期流量周期</dt><dd v-if="data.current_cycle">{{ formatDate(data.current_cycle.starts_at) }} 至 {{ formatDate(data.current_cycle.ends_at) }}</dd><dd v-else>当前周期尚未确认</dd></div></dl>
      </template>
    </template>
    <template v-else>
      <div class="period-selector"><el-radio-group v-model="period" aria-label="统计时间范围"><el-radio-button v-for="option in periods" :key="option.id" :value="option.id">{{ option.label }}</el-radio-button></el-radio-group></div>
      <UsageTimeline :data="data" :period="period" :loading="busy" />
    </template>
  </section>
</template>
<style scoped>
.usage-overview { padding:26px; margin-bottom:22px; }
.quality-line { display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-top:20px; }
.period-selector { margin:22px 0; }
.usage-dates { display:grid; grid-template-columns:2fr 1fr; gap:20px; margin:22px 0 0; }
.usage-dates dt { color:var(--muted,#687887); font-size:13px; margin-bottom:6px; }
.usage-dates dd { margin:0; font-size:14px; }
@media(max-width:640px) { .usage-overview { padding:18px; }.usage-dates { grid-template-columns:1fr; } }
</style>
