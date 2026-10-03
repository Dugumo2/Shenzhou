<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { request, errorMessage } from '../api'
import { formatDate, formatGB, integerBytes } from '../display'
import { reliableUsagePercent } from '../usage-types'
import type { UsageOverviewData, UsagePeriod, UsageBytes } from '../usage-types'

const props = defineProps<{ serviceId: string; refreshKey?: number }>()
const period = ref<UsagePeriod>('current'), data = ref<UsageOverviewData | null>(null)
const busy = ref(false), error = ref('')
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
  <section class="surface usage-overview" aria-labelledby="usage-overview-title" :aria-busy="busy">
    <div class="section-title"><div><p class="eyebrow">这份服务的流量</p><h2 id="usage-overview-title">流量概览</h2></div><el-button :loading="busy" @click="load">刷新统计</el-button></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" />
    <el-skeleton v-if="busy" :rows="4" animated />
    <template v-if="data">
      <div class="quality-line"><el-tag :type="data.quality.state === 'measured' ? 'success' : 'warning'">{{ qualityLabel }}</el-tag><span class="small muted">最后采集：{{ data.quality.collected_at ? formatDate(data.quality.collected_at) : '暂无采集记录' }}</span></div>
      <p class="quality-note">{{ data.quality.message }}</p>
      <dl class="metrics-grid usage-metrics">
        <div><dt>{{ data.quality.state === 'measured' ? '本期扣费流量' : data.quality.state === 'stale' ? '上次确认的本期扣费' : '本期扣费（待核算）' }}</dt><dd :title="data.summary.charged_bytes === null ? undefined : data.summary.charged_bytes + ' 字节'">{{ bytes(data.summary.charged_bytes) }}</dd></div>
        <div><dt>本期已确认上传</dt><dd :title="data.summary.upload_bytes === null ? undefined : data.summary.upload_bytes + ' 字节'">{{ bytes(data.summary.upload_bytes) }}</dd></div>
        <div><dt>本期已确认下载</dt><dd :title="data.summary.download_bytes === null ? undefined : data.summary.download_bytes + ' 字节'">{{ bytes(data.summary.download_bytes) }}</dd></div>
      </dl>
      <p class="small muted">上传、下载为已确认记录中的原始传输量；扣费流量沿记录当时的授权倍率汇总，历史倍率不重算。1 GB = 1,000,000,000 字节。</p>
      <div v-if="percentage !== null" class="usage-progress"><div class="progress-caption"><span>本期已用 {{ percentage }}%</span><strong>剩余 {{ bytes(data.summary.remaining_bytes) }}</strong></div><el-progress :percentage="percentage" :show-text="false" /><p class="small muted">以已应用额度和当前已确认扣费计算。</p></div>
      <dl class="usage-dates"><div><dt>当前账期（上海时间）</dt><dd v-if="data.current_cycle">{{ formatDate(data.current_cycle.starts_at) }} 至 {{ formatDate(data.current_cycle.ends_at) }}</dd><dd v-else>当前账期尚未确认</dd></div><div><dt>下次流量重置</dt><dd>{{ formatDate(data.summary.next_reset_at) }}</dd></div></dl>
      <div class="history-heading"><h3>已确认入账记录</h3><el-radio-group v-model="period" aria-label="入账记录时间范围" :disabled="busy"><el-radio-button v-for="option in periods" :key="option.id" :value="option.id">{{ option.label }}</el-radio-button></el-radio-group></div>
      <p class="small muted">{{ data.history.message }}</p>
      <p v-if="period !== 'current'" class="small muted">入账范围：{{ formatDate(data.history.range_start) }} 至 {{ formatDate(data.history.range_end) }}（上海时间）</p>
      <template v-if="data.history.record_count && data.history.totals">
        <dl class="metrics-grid history-totals"><div><dt>所选范围已确认扣费</dt><dd>{{ bytes(data.history.totals.charged_bytes) }}</dd></div><div><dt>已确认上传</dt><dd>{{ bytes(data.history.totals.upload_bytes) }}</dd></div><div><dt>已确认下载</dt><dd>{{ bytes(data.history.totals.download_bytes) }}</dd></div></dl>
        <div class="usage-table-scroll"><table class="usage-table"><caption>按入账日期汇总（上海时间）</caption><thead><tr><th scope="col">入账日期</th><th scope="col">扣费流量</th><th scope="col">上传</th><th scope="col">下载</th></tr></thead><tbody><tr v-for="day in data.history.days" :key="day.date"><th scope="row">{{ day.date }}</th><td>{{ bytes(day.charged_bytes) }}</td><td>{{ bytes(day.upload_bytes) }}</td><td>{{ bytes(day.download_bytes) }}</td></tr></tbody></table></div>
        <p v-if="data.history.days_truncated" class="small muted">仅列出最近 {{ data.history.day_limit }} 个有记录的入账日期；上方汇总包含所选范围的全部已确认记录。</p>
      </template>
      <p v-else class="inline-note">{{ data.source_type === 'membership' ? '此服务尚无可归属的计量记录，请联系管理员核对。' : '所选范围暂无已确认入账记录。未显示记录不代表没有流量。' }}</p>
      <p v-if="data.history.excluded_record_count" class="quality-note">另有 {{ data.history.excluded_record_count }} 条记录尚未确认计量口径，未计入以上入账汇总。</p>
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
.history-heading { border-top: 1px solid var(--border, #dfe6ed); padding-top: 22px; }
.usage-table-scroll { overflow-x: auto; }
.usage-table { width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 14px; }
.usage-table caption { text-align: left; color: var(--muted, #687887); font-size: 12px; padding-bottom: 10px; }
.usage-table th, .usage-table td { text-align: left; padding: 12px 10px; border-bottom: 1px solid var(--border, #dfe6ed); white-space: nowrap; }
.usage-table thead th { color: var(--muted, #687887); font-weight: 500; }
.usage-table tbody th { font-weight: 500; }
@media (max-width: 640px) { .usage-overview { padding: 18px; } .usage-dates { grid-template-columns: 1fr; } }
</style>
