<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { formatGB, integerBytes } from '../display'
import { quotaGauge, transferShare, historyBars } from '../usageCharts'
import type { ChartMetric } from '../usageCharts'
import type { UsageOverviewData } from '../usage-types'
const props = defineProps<{ data: UsageOverviewData; compact?: boolean }>()
const metric = ref<ChartMetric>('charged'), selectedDate = ref('')
const gauge = computed(() => quotaGauge(props.data)), transfer = computed(() => transferShare(props.data))
const bars = computed(() => historyBars(props.data, metric.value))
const selected = computed(() => bars.value.find(point => point.date === selectedDate.value))
const stride = computed(() => Math.max(1, Math.ceil(bars.value.length / 7)))
const gaugeStyle = computed(() => gauge.value.percent === null ? {} : {
  background: `conic-gradient(${gauge.value.mode === 'recorded' ? '#b7791f' : '#3155df'} ${gauge.value.percent}%, #e8edf5 0)`,
})
const gaugeLabel = computed(() => gauge.value.mode === 'current' ? '当前已用占比' : gauge.value.mode === 'recorded' ? '上次确认占比' : '占比待核算')
function bytes(value: string | null) { const n = integerBytes(value); return n !== null && n > 0n && n < 10_000_000n ? '< 0.01 GB' : formatGB(value, '待核算') }
function pointLabel(point: { date: string; value: string | null }) { return point.date + '：' + (point.value === null ? '暂无已确认记录，不代表零流量' : bytes(point.value)) }
watch(() => [props.data.service_id, props.data.period, metric.value], () => { selectedDate.value = '' })
</script>
<template>
  <div class="dashboard-grid">
    <article class="dashboard-card quota-card" aria-label="套餐额度仪表盘">
      <h3>套餐额度</h3>
      <div class="quota-body">
        <div class="quota-ring" :class="{unknown: gauge.percent === null}" :style="gaugeStyle" role="img" :aria-label="gaugeLabel + (gauge.percent === null ? '' : ' ' + gauge.percent + '%')">
          <div class="ring-center"><span>{{ gaugeLabel }}</span><strong>{{ gauge.percent === null ? '—' : gauge.percent + '%' }}</strong></div>
        </div>
        <dl class="quota-legend"><div><dt>本期额度</dt><dd>{{ bytes(data.summary.quota_bytes) }}</dd></div><div><dt>{{ gauge.mode === 'current' ? '套餐已用' : '已记录用量' }}</dt><dd>{{ bytes(data.summary.charged_bytes) }}</dd></div><div><dt>剩余流量</dt><dd>{{ bytes(data.summary.remaining_bytes) }}</dd></div></dl>
      </div>
      <p class="chart-note">{{ gauge.mode === 'recorded' ? '仅表示上次确认的用量占比；灰色部分不代表当前可用余额。' : gauge.mode === 'unknown' ? '统计或额度尚未确认，暂不绘制占比。' : '按已应用额度及已确认套餐用量计算。' }}</p>
    </article>
    <article class="dashboard-card" aria-label="已确认上传下载构成">
      <h3>已确认传输量</h3><p class="chart-note">本期原始传输量，未乘流量倍率。</p>
      <div class="transfer-total"><strong>{{ transfer ? bytes(transfer.total) : '待核算' }}</strong><span>上传 + 下载</span></div>
      <div v-if="transfer && !transfer.empty" class="transfer-bar" role="img" :aria-label="'已确认上传占比 ' + transfer.upPercent + '%，下载占比 ' + transfer.downPercent + '%'"><span class="upload" :style="{width:transfer.upPercent+'%'}"></span><span class="download" :style="{width:transfer.downPercent+'%'}"></span></div>
      <div v-else class="transfer-empty">{{ transfer ? '已确认记录为零' : '等待可靠统计' }}</div>
      <dl class="transfer-legend"><div><dt><i class="upload"></i>上传</dt><dd>{{ bytes(data.summary.upload_bytes) }}</dd></div><div><dt><i class="download"></i>下载</dt><dd>{{ bytes(data.summary.download_bytes) }}</dd></div></dl>
    </article>
  </div>
  <figure v-if="!compact" class="history-chart" aria-label="按记录日期的流量统计柱图">
    <div class="history-chart-heading"><h3>流量统计图</h3><el-radio-group v-model="metric" aria-label="图表统计口径"><el-radio-button value="charged">套餐用量</el-radio-button><el-radio-button value="transfer">上传 + 下载</el-radio-button></el-radio-group></div>
    <figcaption>按记录日期展示，可能包含延迟上报，不代表当天实际流量。虚线位置表示暂无记录，不能当作零。</figcaption>
    <div v-if="bars.length" class="chart-scroll"><div class="bar-chart" :style="{'--columns': bars.length, minWidth: Math.max(260,bars.length*22)+'px'}">
      <button v-for="(point,index) in bars" :key="point.date" type="button" class="chart-column" :class="{selected:selectedDate===point.date}" :aria-label="pointLabel(point)" :title="pointLabel(point)" :aria-pressed="selectedDate===point.date" @click="selectedDate=point.date">
        <span class="bar-area"><span v-if="point.value === null" class="missing-bar">?</span><span v-else class="actual-bar" :class="{zero:point.value==='0'}" :style="{height:point.height+'%'}"></span></span>
        <span class="date-label">{{ index%stride===0 || index===bars.length-1 ? point.date.slice(5) : '' }}</span>
      </button>
    </div></div>
    <p v-else class="chart-note">当前没有可确定的统计范围，等待核验后展示。</p>
    <p class="chart-readout" aria-live="polite">{{ selected ? pointLabel(selected) : '点击或移到柱形上查看数值；最多显示最近90天。' }}</p>
  </figure>
</template>
<style scoped>
.dashboard-grid { display:grid; grid-template-columns:1.2fr 1fr; gap:20px; margin:24px 0; }
.dashboard-card { border:1px solid #e3e9f2; border-radius:14px; padding:22px; background:linear-gradient(145deg,#f8faff,#fff); min-width:0; }
.dashboard-card h3,.history-chart h3 { margin:0 0 16px; font-size:17px; }
.quota-body { display:flex; align-items:center; gap:28px; }
.quota-ring { width:172px; height:172px; border-radius:50%; display:grid; place-items:center; flex-shrink:0; background:#e8edf5; }
.quota-ring.unknown { background:repeating-conic-gradient(#d8e0ed 0deg 9deg,transparent 9deg 15deg); }
.ring-center { width:134px; height:134px; border-radius:50%; background:#fcfdff; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:7px; }
.ring-center span,.chart-note,.history-chart figcaption { color:#63738b; font-size:12px; line-height:1.7; }
.ring-center strong { font-size:30px; color:#213652; }
.quota-legend { display:grid; gap:13px; margin:0; }
.quota-legend dt,.transfer-legend dt { font-size:12px; color:#687887; }
.quota-legend dd { font-size:19px; font-weight:600; margin:4px 0 0; }
.chart-note { margin:12px 0 0; }
.transfer-total { margin:22px 0; display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }
.transfer-total strong { font-size:30px; }
.transfer-total span { font-size:12px; color:#687887; }
.transfer-bar { display:flex; height:16px; background:#edf1f6; border-radius:8px; overflow:hidden; }
.upload { background:#365ce8; }.download { background:#0c947e; }
.transfer-empty { border:1px dashed #c9d3e1; color:#687887; font-size:12px; padding:8px; text-align:center; border-radius:7px; }
.transfer-legend { display:flex; justify-content:space-between; gap:16px; margin:18px 0 0; }
.transfer-legend dd { margin:6px 0 0; font-size:20px; font-weight:600; }
.transfer-legend i { display:inline-block; width:8px; height:8px; margin-right:6px; border-radius:2px; }
.history-chart { margin:22px 0; border:1px solid #e3e9f2; padding:22px; border-radius:14px; }
.history-chart-heading { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
.history-chart-heading h3 { margin:0; }
.history-chart figcaption { margin:14px 0; }
.chart-scroll { overflow-x:auto; }.bar-chart { display:grid; grid-template-columns:repeat(var(--columns),minmax(0,1fr)); gap:4px; height:200px; }
.chart-column { appearance:none; border:0; background:transparent; padding:0; cursor:pointer; min-width:0; color:#617087; font:inherit; }
.bar-area { height:168px; display:flex; align-items:flex-end; justify-content:center; border-bottom:1px solid #dfe6ef; background:repeating-linear-gradient(to top,transparent 0,transparent 40px,#f0f3f8 41px,#f0f3f8 42px); }
.actual-bar { display:block; width:66%; max-width:42px; min-height:2px; background:#4569e8; border-radius:4px 4px 0 0; }
.actual-bar.zero { height:2px !important; background:#929eaf; }.missing-bar { border:1px dashed #c4cede; border-radius:3px; width:66%; max-width:42px; height:22px; line-height:20px; font-size:10px; }
.chart-column:hover .actual-bar,.chart-column.selected .actual-bar { background:#0c947e; }.chart-column:focus-visible { outline:2px solid #3155df; outline-offset:2px; }
.date-label { display:block; height:24px; line-height:24px; font-size:10px; white-space:nowrap; }
.chart-readout { font-size:12px; margin:10px 0 0; color:#63738b; min-height:20px; }
@media(max-width:850px) { .dashboard-grid { grid-template-columns:1fr; }.quota-body { justify-content:center; } }
@media(max-width:480px) { .dashboard-card,.history-chart { padding:16px; }.quota-body { gap:18px; }.quota-ring { width:144px;height:144px; }.ring-center { width:112px;height:112px; }.ring-center strong { font-size:25px; } }
</style>
