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
const details = computed(() => resourceDetailRows(props.meter.details))
const qualityLabels = { current: '', stale: '更新延迟', error: '更新失败', gap: '统计存在缺口', missing: '暂无记录' }
const hasUpload = computed(() => resourceBytes(props.meter.upload_bytes) !== null)
const hasDownload = computed(() => resourceBytes(props.meter.download_bytes) !== null)
const current = computed(() => quality.value === 'current')
</script>

<template>
  <article class="resource-usage-card" :aria-label="meter.label + '用量'">
    <header class="meter-heading">
      <div><h3>{{ meter.label }}</h3><p class="scope-label">{{ resourceScopeLabel(meter.scope) }}</p></div>
      <span class="source-badge">{{ meter.source_kind === 'official' ? '官方' : '估算' }}</span>
    </header>
    <p v-if="!current" class="quality-state" :class="quality" role="status">{{ qualityLabels[quality] }}</p>
    <dl class="primary-values">
      <div class="used-value"><dt>{{ current ? '已用' : '已记录用量' }}</dt><dd>{{ resourceBytesLabel(meter.used_bytes) }}</dd></div>
      <div><dt>额度</dt><dd>{{ resourceBytesLabel(meter.quota_bytes, '未提供') }}</dd></div>
      <div><dt>{{ current ? '剩余' : '上次记录剩余' }}</dt><dd>{{ resourceBytesLabel(meter.remaining_bytes, '暂不可确认') }}</dd></div>
    </dl>
    <div v-if="percent !== null" class="quota-track" :class="{ previous: !current }" role="img"
      :aria-label="(current ? '已用占比 ' : '上次记录已用占比 ') + percent + '%'"><span :style="{ width: percent + '%' }"></span></div>
    <dl v-if="hasUpload || hasDownload" class="transfer-values">
      <div v-if="hasUpload"><dt>上传</dt><dd>{{ resourceBytesLabel(meter.upload_bytes) }}</dd></div>
      <div v-if="hasDownload"><dt>下载</dt><dd>{{ resourceBytesLabel(meter.download_bytes) }}</dd></div>
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
h3 { margin: 0; font-size: 20px; overflow-wrap: anywhere; }
.scope-label { margin: 6px 0 0; color: var(--muted, #687887); font-size: 12px; }
.source-badge { padding: 4px 9px; border-radius: 6px; background: #edf2ff; color: #3155df; font-size: 12px; white-space: nowrap; }
dl { margin: 0; } dt { color: var(--muted, #687887); font-size: 13px; } dd { margin: 6px 0 0; overflow-wrap: anywhere; }
.primary-values { display: grid; grid-template-columns: 1fr 1fr; gap: 20px 14px; margin: 22px 0 18px; }
.primary-values dd { font-size: 21px; font-weight: 600; }
.used-value { grid-column: 1 / -1; }.used-value dd { font-size: 34px; }
.quota-track { height: 10px; border-radius: 8px; overflow: hidden; background: #e6ebf4; }
.quota-track span { display: block; height: 100%; background: #365ce8; }.quota-track.previous span { background: #b48b43; }
.transfer-values { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 18px; }.transfer-values dd { font-size: 16px; font-weight: 600; }
.time-values { display: grid; gap: 12px; margin-top: 22px; }.time-values>div { display: flex; justify-content: space-between; gap: 14px; }.time-values dd { margin: 0; text-align: right; font-size: 13px; }
.quality-state { margin: 14px 0 0; color: #93631c; font-size: 13px; }.quality-state.error { color: #b84040; }.quality-state.missing { color: var(--muted, #687887); }
.resource-alerts { padding-left: 18px; margin: 18px 0 0; color: #93631c; font-size: 13px; line-height: 1.7; }.resource-alerts .danger { color: #b84040; }
.resource-details { margin-top: 20px; border-top: 1px solid var(--border, #dfe6ed); padding-top: 14px; color: var(--muted, #687887); font-size: 12px; line-height: 1.7; }
summary { cursor: pointer; padding: 4px 0; } summary:focus-visible { outline: 2px solid var(--el-color-primary, #3568d4); outline-offset: 3px; }
.detail-values { display: grid; gap: 12px; margin-top: 14px; }.detail-values dt { font-size: 12px; }.detail-values dd { margin-top: 2px; white-space: pre-wrap; }
@media (max-width: 720px) { .resource-usage-card { padding: 18px; }.used-value dd { font-size: 28px; } }
</style>
