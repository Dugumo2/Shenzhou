<script setup lang="ts">
import { computed } from 'vue'
import { billingPercent, providerQuality, resourceAlerts, resourceBytes, resourceBytesLabel, resourceDateLabel,
  resourceDetailRows, resourceScopeLabel, resourceTimeLabel, transferShare } from '../providerUsage'
import type { ResourceUsageMeter } from '../providerUsage'

const props = defineProps<{ meter: ResourceUsageMeter; now: number }>()
const quality = computed(() => providerQuality(props.meter, props.now))
const alerts = computed(() => resourceAlerts(props.meter, props.now))
const percent = computed(() => billingPercent(props.meter))
const share = computed(() => transferShare(props.meter))
const downloadShare = computed(() => share.value === null ? null : Math.round((100 - share.value) * 100) / 100)
const details = computed(() => resourceDetailRows(props.meter.details))
const qualityLabels = { current: '', stale: '更新延迟', error: '更新失败', gap: '统计存在缺口', missing: '暂无记录' }
const hasUpload = computed(() => resourceBytes(props.meter.upload_bytes) !== null)
const hasDownload = computed(() => resourceBytes(props.meter.download_bytes) !== null)
const current = computed(() => quality.value === 'current')
const ringStyle = computed(() => percent.value === null ? {} : {
  background: `conic-gradient(${current.value ? '#365ce8' : '#b48b43'} ${percent.value}%, #e6ebf4 0)`,
})
</script>

<template>
  <article class="resource-usage-card" :aria-label="meter.label + '用量'">
    <header class="meter-heading">
      <div><h3>{{ meter.label }}</h3><p class="scope-label">{{ resourceScopeLabel(meter.scope) }}</p></div>
      <span class="source-badge">{{ meter.source_kind === 'official' ? '官方' : '估算' }}</span>
    </header>
    <p v-if="!current" class="quality-state" :class="quality" role="status">{{ qualityLabels[quality] }}</p>
    <div class="quota-dashboard">
    <div class="quota-ring" :class="{ unknown: percent === null, previous: !current }" :style="ringStyle"
      role="img" data-chart="quota-gauge" :aria-label="meter.label + '额度占用仪表盘：' + (percent === null ? '暂无可计算比例' : (current ? '已用 ' : '上次记录已用 ') + percent + '%')">
      <div class="ring-center"><span>{{ percent === null ? '暂无比例' : current ? '已用占比' : '上次记录占比' }}</span><strong>{{ percent === null ? '—' : percent + '%' }}</strong></div>
    </div>
    <dl class="primary-values">
      <div class="used-value"><dt>{{ current ? '已用' : '已记录用量' }}</dt><dd>{{ resourceBytesLabel(meter.used_bytes) }}</dd></div>
      <div><dt>额度</dt><dd>{{ resourceBytesLabel(meter.quota_bytes, '未提供') }}</dd></div>
      <div><dt>{{ current ? '剩余' : '上次记录剩余' }}</dt><dd>{{ resourceBytesLabel(meter.remaining_bytes, '暂不可确认') }}</dd></div>
    </dl>
    </div>
    <p v-if="percent !== null && (!current || resourceBytes(meter.remaining_bytes) === null)" class="chart-note">圆环只表示已记录用量占额度的比例，灰色部分不代表当前已确认剩余。</p>
    <div v-if="share !== null" class="transfer-chart" data-chart="transfer-composition">
      <h4>上传 / 下载构成</h4>
      <div class="transfer-track" role="img" :aria-label="meter.label + '传输构成：上传 ' + share + '%，下载 ' + downloadShare + '%'">
        <span class="upload-segment" :style="{ width: share + '%' }"></span><span class="download-segment" :style="{ width: downloadShare + '%' }"></span>
      </div>
    </div>
    <dl v-if="hasUpload || hasDownload" class="transfer-values">
      <div v-if="hasUpload"><dt><i class="legend-upload" aria-hidden="true"></i>上传</dt><dd>{{ resourceBytesLabel(meter.upload_bytes) }}</dd></div>
      <div v-if="hasDownload"><dt><i class="legend-download" aria-hidden="true"></i>下载</dt><dd>{{ resourceBytesLabel(meter.download_bytes) }}</dd></div>
    </dl>
    <dl class="time-values">
      <div><dt>下次重置</dt><dd>{{ resourceTimeLabel(meter.cycle?.next_reset_at ?? null) }}</dd></div>
      <div><dt>到期日期</dt><dd>{{ resourceDateLabel(meter.expires_on) }}</dd></div>
      <div><dt>最后更新</dt><dd>{{ resourceTimeLabel(meter.observed_at) }}</dd></div>
    </dl>
    <ul v-if="alerts.length" class="resource-alerts" aria-label="用量提醒">
      <li v-for="(alert, index) in alerts" :key="alert.code + ':' + index" :class="{ danger: alert.severity === 'error' || alert.severity === 'critical' }">{{ alert.message }}</li>
    </ul>
    <details class="resource-details">
      <summary>统计详情</summary>
      <dl class="detail-values">
        <div><dt>统计范围</dt><dd>{{ resourceScopeLabel(meter.scope) }}</dd></div>
        <div><dt>数据来源</dt><dd>{{ meter.source_kind === 'official' ? '官方' : '估算' }}</dd></div>
        <div v-if="meter.cycle?.starts_at"><dt>周期开始</dt><dd>{{ resourceTimeLabel(meter.cycle.starts_at) }}</dd></div>
        <div v-if="meter.cycle?.ends_at"><dt>周期结束</dt><dd>{{ resourceTimeLabel(meter.cycle.ends_at) }}</dd></div>
        <div><dt>记录有效至</dt><dd>{{ resourceTimeLabel(meter.expires_at) }}</dd></div>
        <div v-if="share !== null"><dt>上传占传输量</dt><dd>{{ share }}%</dd></div>
        <div v-for="(detail, index) in details" :key="index"><dt>{{ detail.label }}</dt><dd>{{ detail.value }}</dd></div>
      </dl>
      <p>时间按上海时区显示；到期日期与流量重置分别记录。</p>
    </details>
  </article>
</template>

<style scoped>
.resource-usage-card { min-width: 0; border: 1px solid var(--border, #dfe6ed); border-radius: 14px; padding: 24px; background: linear-gradient(145deg, #f7faff, #fff); }
.meter-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.meter-heading > div { min-width: 0; }
h3 { margin: 0; font-size: 20px; overflow-wrap: anywhere; }
.scope-label { margin: 6px 0 0; color: var(--muted, #687887); font-size: 12px; }
.source-badge { padding: 4px 9px; border-radius: 6px; background: #edf2ff; color: #3155df; font-size: 12px; white-space: nowrap; }
dl { margin: 0; } dt { color: var(--muted, #687887); font-size: 13px; } dd { margin: 6px 0 0; overflow-wrap: anywhere; }
.quota-dashboard { display:flex; align-items:center; flex-wrap:wrap; gap:24px; margin:24px 0; }
.quota-ring { width:168px; height:168px; flex:0 0 168px; border-radius:50%; display:grid; place-items:center; margin:auto; }
.quota-ring.unknown { background:repeating-conic-gradient(#d8e0ed 0deg 9deg,transparent 9deg 15deg); }
.ring-center { width:130px; height:130px; border-radius:50%; background:#fcfdff; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:8px; }
.ring-center span { font-size:12px; color:var(--muted,#687887); }.ring-center strong { font-size:29px; color:#213652; }
.primary-values { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 14px; flex:1 1 180px; min-width:0; }
.primary-values dd { font-size: 21px; font-weight: 600; }
.used-value { grid-column: 1 / -1; }.used-value dd { font-size: 34px; }
.chart-note { font-size:12px; color:var(--muted,#687887); line-height:1.7; }
.transfer-chart { margin-top:24px; }.transfer-chart h4 { font-size:13px; font-weight:500; margin:0 0 12px; color:var(--muted,#687887); }
.transfer-track { display:flex; height:14px; border-radius:7px; overflow:hidden; background:#e6ebf4; }
.upload-segment,.legend-upload { background:#365ce8; }.download-segment,.legend-download { background:#139b87; }
.transfer-values i { display:inline-block; width:8px; height:8px; border-radius:2px; margin-right:6px; }
.transfer-values { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 18px; }.transfer-values dd { font-size: 16px; font-weight: 600; }
.time-values { display: grid; gap: 12px; margin-top: 22px; }.time-values>div { display: flex; justify-content: space-between; gap: 14px; }.time-values dd { margin: 0; text-align: right; font-size: 13px; }
.quality-state { margin: 14px 0 0; color: #93631c; font-size: 13px; }.quality-state.error { color: #b84040; }.quality-state.missing { color: var(--muted, #687887); }
.resource-alerts { padding-left: 18px; margin: 18px 0 0; color: #93631c; font-size: 13px; line-height: 1.7; }.resource-alerts .danger { color: #b84040; }
.resource-details { margin-top: 20px; border-top: 1px solid var(--border, #dfe6ed); padding-top: 14px; color: var(--muted, #687887); font-size: 12px; line-height: 1.7; }
summary { cursor: pointer; padding: 4px 0; } summary:focus-visible { outline: 2px solid var(--el-color-primary, #3568d4); outline-offset: 3px; }
.detail-values { display: grid; gap: 12px; margin-top: 14px; }.detail-values dt { font-size: 12px; }.detail-values dd { margin-top: 2px; white-space: pre-wrap; }
@media (max-width: 720px) { .resource-usage-card { padding: 18px; }.used-value dd { font-size: 28px; } }
</style>
