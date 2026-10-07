<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import type { ProviderUsage, ResourceUsageMeter } from '../providerUsage'
import { providerQuality, resourceBytesLabel, resourceDateLabel, resourceScopeLabel, resourceTimeLabel } from '../providerUsage'

defineProps<{ data?: ProviderUsage | null }>()
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 30_000) })
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
const labels = { current: '', stale: '更新延迟', error: '更新失败', gap: '统计待核对', missing: '暂无采样' }
function quality(meter: ResourceUsageMeter) { return labels[providerQuality(meter, now.value)] }
</script>

<template>
  <div class="resource-summary" data-usage-scope="resources">
    <p class="summary-caption">按资源查看用量 <span>各资源额度独立计算</span></p>
    <p v-if="data?.error" class="summary-notice" role="status">{{ data.error.message }}</p>
    <p v-if="!data?.meters.length" class="summary-notice">资源统计暂不可读取，稍后可更新查看。</p>
    <article v-for="meter in data?.meters || []" :key="meter.id" class="resource-summary-item">
      <div class="summary-heading"><strong>{{ meter.label }}</strong><span>{{ resourceScopeLabel(meter.scope) }} · {{ meter.source_kind === 'official' ? '官方' : '估算' }}</span></div>
      <p v-if="quality(meter)" class="summary-notice" role="status">{{ quality(meter) }}，显示已有记录。</p>
      <dl class="summary-values">
        <div><dt>{{ quality(meter) ? '已记录用量' : '已用' }} / 额度</dt><dd>{{ resourceBytesLabel(meter.used_bytes) }} <span>/ {{ resourceBytesLabel(meter.quota_bytes, '未提供') }}</span></dd></div>
        <div><dt>{{ quality(meter) ? '上次记录剩余' : '剩余' }}</dt><dd>{{ resourceBytesLabel(meter.remaining_bytes, '暂不可确认') }}</dd></div>
      </dl>
      <dl class="summary-dates"><div><dt>下次重置</dt><dd>{{ resourceTimeLabel(meter.cycle?.next_reset_at ?? null) }}</dd></div><div><dt>到期</dt><dd>{{ resourceDateLabel(meter.expires_on) }}</dd></div></dl>
      <p class="summary-sampled">采样：{{ resourceTimeLabel(meter.observed_at) }}</p>
    </article>
  </div>
</template>

<style scoped>
.resource-summary { margin: 22px 0 18px; min-width: 0; }
.summary-caption { color: var(--text, #243a54); font-weight: 600; margin: 0 0 12px; }
.summary-caption span { display: block; margin-top: 5px; font-size: 12px; font-weight: 400; color: var(--muted, #687887); }
.resource-summary-item { border-top: 1px solid var(--border, #e3e9f2); padding: 16px 0 4px; }
.resource-summary-item + .resource-summary-item { margin-top: 14px; }
.summary-heading { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.summary-heading strong { overflow-wrap: anywhere; }.summary-heading span, dt, .summary-sampled { font-size: 12px; color: var(--muted, #687887); }
dl { margin: 14px 0 0; display: grid; gap: 12px; grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr); }
dd { margin: 5px 0 0; overflow-wrap: anywhere; font-size: 14px; }.summary-values dd { font-size: 18px; font-weight: 600; }.summary-values dd span { font-size: 13px; color: var(--muted, #687887); font-weight: 400; }
.summary-sampled { margin: 12px 0 0; }.summary-notice { color: #8b621f; font-size: 12px; line-height: 1.7; margin: 8px 0; }
@media(max-width: 420px) { dl { grid-template-columns: 1fr; gap: 10px; } }
</style>
