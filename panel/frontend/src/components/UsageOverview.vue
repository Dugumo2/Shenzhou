<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { request, errorMessage } from '../api'
import { formatDate, formatGB, integerBytes } from '../display'
import { reliableUsagePercent } from '../usage-types'
import UsageDashboard from './UsageDashboard.vue'
import type { UsageOverviewData, UsagePeriod, UsageBytes } from '../usage-types'

const props = defineProps<{ serviceId: string; refreshKey?: number; summaryOnly?: boolean }>()
const period = ref<UsagePeriod>('current'), data = ref<UsageOverviewData | null>(null)
const busy = ref(false), error = ref('')
const recordsOpen = ref(false)
const headingId = computed(() => 'usage-' + props.serviceId + (props.summaryOnly ? '-summary' : '-detail'))
const periods: { id: UsagePeriod; label: string }[] = [{ id: 'current', label: '本期' }, { id: '7d', label: '近7天' }, { id: '30d', label: '近30天' }]
const qualityLabel = computed(() => ({ measured: '已取得当前样本', stale: '统计已过期', gap: '统计存在缺口', unknown: '暂无可靠统计' })[data.value?.quality.state || 'unknown'])
const percentage = computed(() => data.value ? reliableUsagePercent(data.value) : null)
let generation = 0
async function load() {
  const current = ++generation
  busy.value = true; error.value = ''; data.value = null
  try {
    const result = await request<UsageOverviewData>('/me/services/' + encodeURIComponent(props.serviceId) + '/usage?period=' + period.value)
    if (current === generation) data.value = result
  } catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}
watch(() => [props.serviceId, props.refreshKey, period.value], () => { void load() }, { immediate: true })
onBeforeUnmount(() => { generation++ })
function bytes(value: UsageBytes, unknown = '暂无可靠统计') {
  const parsed = integerBytes(value)
  // 非零小样本不能因GB两位显示而伪装成0GB。
  return parsed !== null && parsed > 0n && parsed < 10_000_000n ? '< 0.01 GB' : formatGB(value, unknown)
}
</script>

<template>
  <section class="surface usage-overview" :aria-labelledby="headingId" :aria-busy="busy">
    <div class="section-title"><div><h2 :id="headingId">{{ summaryOnly ? '流量仪表盘' : '流量用量与统计图' }}</h2><p class="small muted">查看这份服务的额度占用与传输构成。</p></div><el-button :loading="busy" @click="load">刷新统计</el-button></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-skeleton v-if="busy" :rows="4" animated />
    <template v-if="data">
      <div class="quality-line"><el-tag :type="data.quality.state === 'measured' ? 'success' : 'warning'">{{ qualityLabel }}</el-tag><span class="small muted">最后采集：{{ data.quality.collected_at ? formatDate(data.quality.collected_at) : '暂无采集记录' }}</span></div>
      <p class="quality-note">{{ data.quality.message }}</p>
      <div v-if="!summaryOnly" class="history-heading"><h3>统计范围</h3><el-radio-group v-model="period" aria-label="统计记录时间范围" :disabled="busy"><el-radio-button v-for="option in periods" :key="option.id" :value="option.id">{{ option.label }}</el-radio-button></el-radio-group></div>
      <UsageDashboard :data="data" :compact="summaryOnly" />
      <p class="small muted">套餐用量按上传、下载和授权倍率折算，原始传输量另列。</p>
      <dl v-if="!summaryOnly" class="usage-dates"><div><dt>本期流量周期</dt><dd v-if="data.current_cycle">{{ formatDate(data.current_cycle.starts_at) }} 至 {{ formatDate(data.current_cycle.ends_at) }}</dd><dd v-else>当前周期尚未确认</dd></div><div><dt>下次流量重置</dt><dd>{{ formatDate(data.summary.next_reset_at) }}</dd></div></dl>
      <details v-if="!summaryOnly" class="usage-records" :open="recordsOpen" @toggle="recordsOpen = ($event.target as HTMLDetailsElement).open"><summary>查看详细统计</summary>
      <div class="history-heading"><h3>流量统计记录</h3></div>
      <p class="small muted">按统计记录的日期汇总，可能包含延迟上报的用量，不代表当天实际使用量；没有记录的日期不代表零流量。</p>
      <p v-if="period !== 'current'" class="small muted">记录范围：{{ formatDate(data.history.range_start) }} 至 {{ formatDate(data.history.range_end) }}</p>
      <template v-if="data.history.record_count && data.history.totals">
        <div class="usage-table-scroll"><table class="usage-table"><caption>所选范围已确认套餐用量 {{ bytes(data.history.totals.charged_bytes) }}</caption><thead><tr><th scope="col">记录日期</th><th scope="col">套餐用量</th><th scope="col">上传</th><th scope="col">下载</th></tr></thead><tbody><tr v-for="day in data.history.days" :key="day.date"><th scope="row">{{ day.date }}</th><td>{{ bytes(day.charged_bytes) }}</td><td>{{ bytes(day.upload_bytes) }}</td><td>{{ bytes(day.download_bytes) }}</td></tr></tbody></table></div>
        <p v-if="data.history.days_truncated" class="small muted">仅列出最近 {{ data.history.day_limit }} 个有记录的日期；汇总包含所选范围的全部已确认记录。</p>
      </template>
      <p v-else class="inline-note">{{ data.source_type === 'membership' ? '此服务暂无可靠统计，请联系管理员核对。' : '所选范围暂无已确认记录，不代表没有使用流量。' }}</p>
      <p v-if="data.history.excluded_record_count" class="quality-note">另有 {{ data.history.excluded_record_count }} 条待核验记录，暂未计入汇总。</p>
      <p class="small muted">上传和下载显示原始传输量；套餐用量按当时的倍率折算，历史不会重新计算。1 GB = 1,000,000,000 字节。</p>
      </details>
    </template>
  </section>
</template>

<style scoped>
.usage-overview { padding: 26px; margin-bottom: 22px; }
.quality-line, .progress-caption, .history-heading { display: flex; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap; }
.quality-line { justify-content: flex-start; margin-top: 20px; }
.usage-metrics, .history-totals { margin: 22px 0 12px; }
.usage-progress { margin: 22px 0; }
.progress-caption { margin-bottom: 10px; }
.usage-dates { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; margin: 22px 0; }
.usage-dates dt { color: var(--muted, #687887); font-size: 13px; margin-bottom: 6px; }
.usage-dates dd { margin: 0; font-size: 14px; }
.usage-records { border-top: 1px solid var(--border, #dfe6ed); padding-top: 18px; }
.usage-records summary { color: var(--el-color-primary); cursor: pointer; font-size: 14px; }
.history-heading { padding-top: 18px; }
.usage-table-scroll { overflow-x: auto; }
.usage-table { width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 14px; }
.usage-table caption { text-align: left; color: var(--muted, #687887); font-size: 12px; padding-bottom: 10px; }
.usage-table th, .usage-table td { text-align: left; padding: 12px 10px; border-bottom: 1px solid var(--border, #dfe6ed); white-space: nowrap; }
.usage-table thead th { color: var(--muted, #687887); font-weight: 500; }
.usage-table tbody th { font-weight: 500; }
@media (max-width: 640px) { .usage-overview { padding: 18px; } .usage-dates { grid-template-columns: 1fr; } }
</style>
