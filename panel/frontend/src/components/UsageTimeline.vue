<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { formatDate, formatGB, integerBytes } from '../display'
import { emptyCalendar, selectedTimeseries, timeBars, timeMetricLabels } from '../usageTimeseries'
import type { TimeMetric } from '../usageTimeseries'
import type { UsageOverviewData, CalendarPeriod } from '../usage-types'
const props = defineProps<{ data: UsageOverviewData | null; period: CalendarPeriod; loading?: boolean }>()
const metric = ref<TimeMetric>('charged_bytes'), selectedStart = ref(''), hoveredStart = ref(''), focusedStart = ref(''), linesOpen = ref(false)
const series = computed(() => selectedTimeseries(props.data, props.period))
const initialLoading = computed(() => !!props.loading && !series.value)
const lineUsage = computed(() => {
  const lines = props.data?.line_usage
  return lines && series.value && lines.service_id === props.data?.service_id && lines.period === props.period && lines.summary_revision === series.value.summary_revision ? lines : null
})
const buckets = computed(() => series.value?.buckets ?? emptyCalendar(props.period, props.data?.generated_at || new Date().toISOString()))
const bars = computed(() => timeBars(buckets.value, metric.value))
const activeStart = computed(() => selectedStart.value || hoveredStart.value || focusedStart.value)
const selected = computed(() => bars.value.find(item => item.start === activeStart.value))
const totalLabel = computed(() => series.value?.totals.state === 'complete' ? '所选范围合计' : '所选范围已记录')
const periodLabel = computed(() => ({ '7d': '近7天', month: '本月', year: '今年' })[props.period])
function bytes(value: string | null | undefined) { const n = integerBytes(value ?? null); return n !== null && n > 0n && n < 10_000_000n ? '< 0.01 GB' : formatGB(value ?? null, '未记录') }
function dateLabel(start: string) { return start.slice(0, props.period === 'year' ? 7 : 10) }
function exactBytes(value: string | null) { const n = integerBytes(value); return n === null ? '' : n.toLocaleString('zh-CN') + ' 字节' }
function pointValue(point: typeof bars.value[number]) { return point.state === 'future' ? '未到该时段' : point.value === null ? '未记录' : bytes(point.value) }
function label(point: typeof bars.value[number]) {
  const amount = point.state === 'future' ? '未到该时段' : point.value === null ? '此时段未记录' : bytes(point.value) + (point.state === 'partial' ? '，已记录部分' : '')
  return dateLabel(point.start) + '：' + amount + '，' + timeMetricLabels[metric.value] + (point.value !== null ? '，' + exactBytes(point.value) : '') + (point.is_open ? '，截至当前采样' : '')
}
function clearSelection() { selectedStart.value = ''; hoveredStart.value = ''; focusedStart.value = '' }
function hoverPoint(start: string, event: PointerEvent) { if (event.pointerType !== 'touch') hoveredStart.value = start }
function toggleSelection(start: string) { if (selectedStart.value === start) clearSelection(); else selectedStart.value = start }
watch(() => [props.period, props.data?.service_id, series.value?.range_start], clearSelection)
</script>
<template>
  <div class="timeline" :aria-busy="loading" @keydown.esc="clearSelection">
    <div v-if="initialLoading" class="timeline-loading" data-testid="usage-timeline-loading" role="status">
      <h3>{{ periodLabel }}使用情况</h3><p>正在加载用量…</p><div class="loading-bars" aria-hidden="true"></div>
    </div>
    <template v-else>
    <div class="range-totals"><div><p class="small muted">{{ totalLabel }} · {{ periodLabel }}</p><strong>{{ bytes(series?.totals.charged_bytes) }}</strong><span class="small muted">套餐消耗</span></div><dl><div><dt>原始上传</dt><dd>{{ bytes(series?.totals.upload_bytes) }}</dd></div><div><dt>原始下载</dt><dd>{{ bytes(series?.totals.download_bytes) }}</dd></div></dl></div>
    <figure class="history-chart" aria-label="套餐消耗时间柱图" data-chart="usage-timeseries">
      <div class="chart-heading"><h3>{{ periodLabel }}使用情况</h3><el-radio-group v-model="metric" aria-label="图表统计口径"><el-radio-button v-for="(name,key) in timeMetricLabels" :key="key" :value="key">{{ name }}</el-radio-button></el-radio-group></div>
      <figcaption>悬停、聚焦或点选柱形查看用量。</figcaption>
      <div class="chart-scroll"><div class="bar-chart" :style="{ '--columns': bars.length, minWidth: Math.max(260, bars.length * 25) + 'px' }">
        <button v-for="point in bars" :key="point.start" type="button" class="chart-column" :class="[point.state, {selected:activeStart===point.start}]" data-testid="usage-time-bar" :data-start="point.start" :data-state="point.state" :aria-label="label(point)" :aria-pressed="selectedStart===point.start" @pointerenter="hoverPoint(point.start, $event)" @pointerleave="hoveredStart=''" @focus="focusedStart=point.start" @blur="focusedStart=''" @click="toggleSelection(point.start)">
          <span class="bar-area"><span v-if="point.state==='future'" class="future-bar">·</span><span v-else-if="point.value===null" class="missing-bar">未记录</span><span v-else class="actual-bar" :class="{zero:point.value==='0'}" :style="{height:point.height+'%'}"></span></span>
          <span class="date-label">{{ period==='year' ? Number(point.start.slice(5,7)) + '月' : Number(point.start.slice(8,10)) + '日' }}</span>
        </button>
      </div></div>
      <div class="point-card-slot" aria-live="polite" aria-atomic="true">
        <div v-if="selected" class="point-card" data-testid="usage-point-card" :data-pinned="!!selectedStart">
          <div class="point-card-heading"><span data-testid="usage-point-date">{{ dateLabel(selected.start) }}</span><span class="small muted">{{ selectedStart ? '已固定 · 再点或 Esc 取消' : '点选可固定' }}</span></div>
          <span class="point-metric" data-testid="usage-point-metric">{{ timeMetricLabels[metric] }}</span>
          <strong class="point-value" data-testid="usage-point-value">{{ pointValue(selected) }}</strong>
          <span v-if="selected.value!==null" class="point-exact" data-testid="usage-point-exact">{{ exactBytes(selected.value) }}</span>
          <span v-if="selected.state==='partial'" class="small muted">已记录部分</span>
        </div>
        <div v-else class="point-card-placeholder">选择一个日期查看详细用量</div>
      </div>
      <p v-if="!series || series.totals.state==='missing'" class="chart-note">此时段未记录；暂无可确认的范围合计。</p>
      <p v-if="series" class="small muted">统计截至 {{ formatDate(series.as_of) }}</p>
    </figure>
    <p class="small muted">套餐消耗按当时授权倍率折算；原始上传与下载未乘倍率。1 GB = 1,000,000,000 字节。</p>
    <el-button plain @click="linesOpen=true">查看线路使用明细</el-button>
    <el-drawer v-model="linesOpen" title="线路使用明细" size="min(900px, 100vw)" destroy-on-close>
      <p class="muted">{{ periodLabel }} · 与当前时间图使用相同统计范围</p>
      <template v-if="lineUsage?.lines.length">
        <p>{{ totalLabel }}：{{ bytes(lineUsage.totals.charged_bytes) }}</p>
        <div class="line-scroll"><table><caption>原始上传、下载及当时授权倍率</caption><thead><tr><th>线路 / 节点</th><th>倍率及生效范围</th><th>上传</th><th>下载</th><th>套餐消耗</th></tr></thead><tbody><tr v-for="(line,index) in lineUsage.lines" :key="line.line_id + ':' + index"><td>{{ line.line_name }}<br><span class="small muted">{{ line.node_name }}</span></td><td>×{{ line.multiplier }}<br><span class="small muted">{{ formatDate(line.effective_from) }} 至 {{ line.effective_to ? formatDate(line.effective_to) : '持续有效' }}</span></td><td>{{ bytes(line.upload_bytes) }}</td><td>{{ bytes(line.download_bytes) }}</td><td>{{ bytes(line.charged_bytes) }}</td></tr></tbody></table></div>
      </template>
      <p v-else>此范围暂无可确认的线路明细。</p>
    </el-drawer>
    </template>
  </div>
</template>
<style scoped>
.range-totals { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:22px; padding:22px; background:#f6f8fd; border-radius:14px; }
.line-scroll { overflow-x:auto; }table { width:100%; border-collapse:collapse; font-size:14px; }caption { text-align:left; color:#687887; font-size:12px; padding:12px 0; }th,td { text-align:left; padding:14px 10px; border-bottom:1px solid #e3e9f2; white-space:nowrap; }
.range-totals strong { display:block; font-size:32px; color:#213652; margin:8px 0; }.range-totals p { margin:0; }
.range-totals dl { display:flex; gap:32px; margin:0; }.range-totals dt { color:#687887; font-size:12px; }.range-totals dd { margin:8px 0 0; font-size:19px; font-weight:600; }
.timeline { min-width:0; }.history-chart { margin:22px 0; padding:22px; border:1px solid #e3e9f2; border-radius:14px; min-width:0; }
.chart-heading { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:14px; }.chart-heading h3 { margin:0; }
figcaption,.chart-note { color:#63738b; font-size:12px; line-height:1.7; }figcaption { margin:16px 0; }
.chart-scroll { overflow-x:auto; }.bar-chart { display:grid; grid-template-columns:repeat(var(--columns),minmax(0,1fr)); gap:5px; height:220px; }
.chart-column { appearance:none; border:0; background:transparent; padding:0; cursor:pointer; min-width:0; color:#617087; font:inherit; }
.bar-area { height:180px; display:flex; align-items:flex-end; justify-content:center; border-bottom:1px solid #dfe6ef; background:repeating-linear-gradient(to top,transparent 0,transparent 43px,#f0f3f8 44px,#f0f3f8 45px); }
.actual-bar { display:block; width:66%; max-width:48px; min-height:3px; background:#4569e8; border-radius:4px 4px 0 0; }.actual-bar.zero { height:3px !important; background:#78869d; }.partial .actual-bar { background:#6b86e6; }
.missing-bar { border:1px dashed #bac6d9; border-radius:3px; padding:3px 0; font-size:9px; width:90%; max-width:48px; }.future-bar { color:#9da8ba; }.future .bar-area { background:#fbfcfe; }
.date-label { display:block; height:28px; line-height:28px; font-size:11px; white-space:nowrap; }.chart-column.selected { color:#2446c8; }.chart-column.selected .bar-area { box-shadow:inset 0 0 0 2px #90a5f5; background-color:#edf1ff; }.chart-column.selected .date-label { background:#3155df; color:white; border-radius:4px; font-weight:700; }.chart-column:focus-visible { outline:2px solid #3155df; outline-offset:-2px; }
.point-card-slot { min-height:180px; margin:8px 0 0; }.point-card { box-sizing:border-box; display:flex; flex-direction:column; gap:5px; padding:14px 16px; border:1px solid #b8c8f7; border-radius:10px; background:#f3f6ff; color:#213652; overflow-wrap:anywhere; min-width:0; }.point-card-heading { display:flex; flex-wrap:wrap; gap:6px 16px; justify-content:space-between; font-size:14px; font-weight:600; }.point-metric { font-size:12px; color:#556985; margin-top:3px; }.point-value { font-size:26px; line-height:1.25; }.point-exact { font-size:12px; color:#556985; font-variant-numeric:tabular-nums; }.point-card-placeholder { color:#687887; font-size:12px; padding:18px 0; }.timeline-loading { padding:22px; border:1px solid #e3e9f2; border-radius:14px; color:#63738b; }.loading-bars { height:260px; border-radius:8px; background:linear-gradient(180deg,#f6f8fd,#e9eef8); }
@media(max-width:640px) { .history-chart,.range-totals { padding:16px; }.range-totals strong { font-size:28px; }.range-totals dl { gap:24px; }.chart-heading :deep(.el-radio-button__inner) { padding:8px 10px; } }
</style>
