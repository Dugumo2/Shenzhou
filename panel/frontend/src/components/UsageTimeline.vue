<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { formatDate, formatGB, integerBytes } from '../display'
import { emptyCalendar, selectedTimeseries, timeBars, timeMetricLabels } from '../usageTimeseries'
import type { TimeMetric } from '../usageTimeseries'
import type { UsageOverviewData, CalendarPeriod } from '../usage-types'
const props = defineProps<{ data: UsageOverviewData | null; period: CalendarPeriod; loading?: boolean }>()
const metric = ref<TimeMetric>('charged_bytes'), selectedStart = ref(''), linesOpen = ref(false)
const series = computed(() => selectedTimeseries(props.data, props.period))
const lineUsage = computed(() => {
  const lines = props.data?.line_usage
  return lines && series.value && lines.service_id === props.data?.service_id && lines.period === props.period && lines.summary_revision === series.value.summary_revision ? lines : null
})
const buckets = computed(() => series.value?.buckets ?? emptyCalendar(props.period, props.data?.generated_at || new Date().toISOString()))
const bars = computed(() => timeBars(buckets.value, metric.value))
const selected = computed(() => bars.value.find(item => item.start === selectedStart.value))
const totalLabel = computed(() => series.value?.totals.state === 'complete' ? '所选范围合计' : '所选范围已记录')
const periodLabel = computed(() => ({ '7d': '近7天', month: '本月', year: '今年' })[props.period])
function bytes(value: string | null | undefined) { const n = integerBytes(value ?? null); return n !== null && n > 0n && n < 10_000_000n ? '< 0.01 GB' : formatGB(value ?? null, '未记录') }
function label(point: typeof bars.value[number]) {
  const date = props.period === 'year' ? point.start.slice(0, 7) : point.start.slice(0, 10)
  const amount = point.state === 'future' ? '未到该时段' : point.value === null ? '此时段未记录' : bytes(point.value) + (point.state === 'partial' ? '，已记录部分' : '')
  return date + '：' + amount + (point.is_open ? '，截至当前采样' : '')
}
watch(() => [props.period, metric.value, props.data?.service_id], () => { selectedStart.value = '' })
</script>
<template>
  <div class="timeline" :aria-busy="loading">
    <div class="range-totals"><div><p class="small muted">{{ totalLabel }} · {{ periodLabel }}</p><strong>{{ bytes(series?.totals.charged_bytes) }}</strong><span class="small muted">套餐消耗</span></div><dl><div><dt>原始上传</dt><dd>{{ bytes(series?.totals.upload_bytes) }}</dd></div><div><dt>原始下载</dt><dd>{{ bytes(series?.totals.download_bytes) }}</dd></div></dl></div>
    <figure class="history-chart" aria-label="套餐消耗时间柱图" data-chart="usage-timeseries">
      <div class="chart-heading"><h3>{{ periodLabel }}使用情况</h3><el-radio-group v-model="metric" aria-label="图表统计口径"><el-radio-button v-for="(name,key) in timeMetricLabels" :key="key" :value="key">{{ name }}</el-radio-button></el-radio-group></div>
      <figcaption>按实际采样区间统计。未记录与已测为零分别显示，未来时段留空。</figcaption>
      <div class="chart-scroll"><div class="bar-chart" :style="{ '--columns': bars.length, minWidth: Math.max(260, bars.length * 25) + 'px' }">
        <button v-for="point in bars" :key="point.start" type="button" class="chart-column" :class="[point.state, {selected:selectedStart===point.start}]" :data-state="point.state" :aria-label="label(point)" :title="label(point)" :aria-pressed="selectedStart===point.start" @click="selectedStart=point.start">
          <span class="bar-area"><span v-if="point.state==='future'" class="future-bar">·</span><span v-else-if="point.value===null" class="missing-bar">未记录</span><span v-else class="actual-bar" :class="{zero:point.value==='0'}" :style="{height:point.height+'%'}"></span></span>
          <span class="date-label">{{ period==='year' ? Number(point.start.slice(5,7)) + '月' : Number(point.start.slice(8,10)) + '日' }}</span>
        </button>
      </div></div>
      <p class="chart-readout" aria-live="polite">{{ selected ? label(selected) : '点击柱形查看日期、用量和记录状态。' }}</p>
      <p v-if="!series || series.totals.state==='missing'" class="chart-note">此时段未记录；暂无可确认的范围合计。</p>
      <p v-else-if="series.totals.state==='partial'" class="chart-note">{{ series.message_code === 'sampling_pending' ? '当前时段持续更新，显示最近采样记录。' : '部分时段缺少记录，以上仅为已记录用量。' }}</p>
      <p v-if="series" class="small muted">统计截至 {{ formatDate(series.as_of) }}</p>
      <p v-if="series?.unallocated.length" class="chart-note">另有 {{ bytes(series.totals.unallocated_charged_bytes) }} 已计入范围合计，尚不能精确分配到某个日期。</p>
      <p v-if="series?.boundary_pending_count" class="chart-note">有 {{ series.boundary_pending_count }} 个采样区间跨出当前范围，待核对后计入，未估算补齐。</p>
    </figure>
    <p class="small muted">套餐消耗按当时授权倍率折算；原始上传与下载未乘倍率。1 GB = 1,000,000,000 字节。</p>
    <el-button plain @click="linesOpen=true">查看线路使用明细</el-button>
    <el-drawer v-model="linesOpen" title="线路使用明细" size="min(900px, 100vw)" destroy-on-close>
      <p class="muted">{{ periodLabel }} · 与当前时间图使用相同统计范围</p>
      <template v-if="lineUsage?.lines.length">
        <p>{{ totalLabel }}：{{ bytes(lineUsage.totals.charged_bytes) }}</p>
        <div class="line-scroll"><table><caption>原始上传、下载及当时授权倍率</caption><thead><tr><th>线路 / 节点</th><th>倍率及生效范围</th><th>上传</th><th>下载</th><th>套餐消耗</th></tr></thead><tbody><tr v-for="(line,index) in lineUsage.lines" :key="line.line_id + ':' + index"><td>{{ line.line_name }}<br><span class="small muted">{{ line.node_name }}</span></td><td>×{{ line.multiplier }}<br><span class="small muted">{{ formatDate(line.effective_from) }} 至 {{ line.effective_to ? formatDate(line.effective_to) : '持续有效' }}</span></td><td>{{ bytes(line.upload_bytes) }}</td><td>{{ bytes(line.download_bytes) }}</td><td>{{ bytes(line.charged_bytes) }}</td></tr></tbody></table></div>
        <p v-if="lineUsage.totals.state==='partial'" class="small muted">{{ lineUsage.message_code === 'sampling_pending' ? '当前时段持续更新，显示最近采样记录。' : '仅含已记录部分，缺失时段未补齐。' }}</p>
      </template>
      <p v-else>此范围暂无可确认的线路明细。</p>
    </el-drawer>
  </div>
</template>
<style scoped>
.range-totals { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:22px; padding:22px; background:#f6f8fd; border-radius:14px; }
.line-scroll { overflow-x:auto; }table { width:100%; border-collapse:collapse; font-size:14px; }caption { text-align:left; color:#687887; font-size:12px; padding:12px 0; }th,td { text-align:left; padding:14px 10px; border-bottom:1px solid #e3e9f2; white-space:nowrap; }
.range-totals strong { display:block; font-size:32px; color:#213652; margin:8px 0; }.range-totals p { margin:0; }
.range-totals dl { display:flex; gap:32px; margin:0; }.range-totals dt { color:#687887; font-size:12px; }.range-totals dd { margin:8px 0 0; font-size:19px; font-weight:600; }
.history-chart { margin:22px 0; padding:22px; border:1px solid #e3e9f2; border-radius:14px; }
.chart-heading { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:14px; }.chart-heading h3 { margin:0; }
figcaption,.chart-note,.chart-readout { color:#63738b; font-size:12px; line-height:1.7; }figcaption { margin:16px 0; }
.chart-scroll { overflow-x:auto; }.bar-chart { display:grid; grid-template-columns:repeat(var(--columns),minmax(0,1fr)); gap:5px; height:220px; }
.chart-column { appearance:none; border:0; background:transparent; padding:0; cursor:pointer; min-width:0; color:#617087; font:inherit; }
.bar-area { height:180px; display:flex; align-items:flex-end; justify-content:center; border-bottom:1px solid #dfe6ef; background:repeating-linear-gradient(to top,transparent 0,transparent 43px,#f0f3f8 44px,#f0f3f8 45px); }
.actual-bar { display:block; width:66%; max-width:48px; min-height:3px; background:#4569e8; border-radius:4px 4px 0 0; }.actual-bar.zero { height:3px !important; background:#78869d; }.partial .actual-bar { background:repeating-linear-gradient(135deg,#4569e8 0,#4569e8 5px,#7f98ef 5px,#7f98ef 8px); }
.missing-bar { border:1px dashed #bac6d9; border-radius:3px; padding:3px 0; font-size:9px; width:90%; max-width:48px; }.future-bar { color:#9da8ba; }.future .bar-area { background:#fbfcfe; }
.date-label { display:block; height:28px; line-height:28px; font-size:11px; white-space:nowrap; }.chart-column:hover,.chart-column.selected { color:#3155df; }.chart-column:focus-visible { outline:2px solid #3155df; outline-offset:2px; }.chart-readout { min-height:21px; margin:8px 0; }
@media(max-width:640px) { .history-chart,.range-totals { padding:16px; }.range-totals strong { font-size:28px; }.range-totals dl { gap:24px; }.chart-heading :deep(.el-radio-button__inner) { padding:8px 10px; } }
</style>
