<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { formatDate, formatGB } from '../display'
import { billingPercent, providerQuality, residentialShare } from '../providerUsage'
import type { ProviderMeter, ProviderUsage } from '../providerUsage'
const props = defineProps<{ data: ProviderUsage }>()
const now = ref(Date.now())
const timer = setInterval(() => { now.value = Date.now() }, 30000)
onBeforeUnmount(() => clearInterval(timer))
const percent = computed(() => billingPercent(props.data.bwh))
const share = computed(() => residentialShare(props.data.residential))
const labels: Record<string, string> = { estimate: '累计估算', provider_reported: '官方账单', missing: '暂无记录', stale: '统计已过期', error: '更新失败' }
const quality = (meter: ProviderMeter) => providerQuality(meter, now.value)
const bytes = (value: string | null | undefined) => formatGB(value ?? null, '暂无记录')
</script>
<template>
  <div class="provider-usage" aria-label="已接入来源流量">
    <p class="source-note">HOME 显示住宅出口累计记录，BWH 显示服务器官方账单。两项可能覆盖同一段中转流量，不能相加或相减。</p>
    <div class="meter-grid">
      <article class="meter-card" aria-label="HOME住宅流量">
        <header><h3>HOME</h3><span class="quality" :class="quality(data.residential)">{{ labels[quality(data.residential)] }}</span></header>
        <p class="subtitle">住宅出口 · 从开始记录起累计</p>
        <strong class="main-value">{{ bytes(data.residential.used_bytes) }}</strong>
        <div v-if="share !== null" class="transfer" role="img" :aria-label="'上传占累计流量 ' + share + '%'"><span :style="{ width: share + '%' }"></span></div>
        <dl class="values"><div><dt>上传</dt><dd>{{ bytes(data.residential.upload_bytes) }}</dd></div><div><dt>下载</dt><dd>{{ bytes(data.residential.download_bytes) }}</dd></div></dl>
        <p class="detail">开始记录：{{ data.residential.estimate_start ? formatDate(data.residential.estimate_start) : '未确认' }}</p>
        <p class="detail">最后更新：{{ data.residential.updated_at ? formatDate(data.residential.updated_at) : '暂无记录' }}</p>
        <p class="note">开始记录前的用量未计入；这里不推算本月剩余流量。</p>
        <p v-if="data.residential.collection_gaps" class="note warning">存在 {{ data.residential.collection_gaps }} 次采集缺口，累计值可能少于实际用量。</p>
      </article>
      <article class="meter-card" aria-label="BWH官方账单流量">
        <header><h3>BWH</h3><span class="quality" :class="quality(data.bwh)">{{ labels[quality(data.bwh)] }}</span></header>
        <p class="subtitle">本期服务器账单 · BWG 官方 API</p>
        <strong class="main-value">{{ bytes(data.bwh.used_bytes) }}</strong>
        <div v-if="percent !== null" class="billing-track" role="img" :aria-label="'官方账单已用占比 ' + percent + '%'"><span :class="{ old: quality(data.bwh) !== 'provider_reported' }" :style="{ width: percent + '%' }"></span></div>
        <dl class="values"><div><dt>本期总额</dt><dd>{{ bytes(data.bwh.total_bytes) }}</dd></div><div><dt>{{ quality(data.bwh) === 'provider_reported' ? '官方剩余' : '上次记录剩余' }}</dt><dd>{{ bytes(data.bwh.remaining_bytes) }}</dd></div></dl>
        <p class="detail">下次重置：{{ data.bwh.reset_at ? formatDate(data.bwh.reset_at) : '未确认' }}</p>
        <p class="detail">最后更新：{{ data.bwh.updated_at ? formatDate(data.bwh.updated_at) : '暂无记录' }}</p>
        <p class="note">整台服务器的计费用量，不等于 BWH 直出线路或个人套餐的独立用量。</p>
      </article>
    </div>
    <p class="source-note">各来源保留自己的更新时间。刷新读取已采集数据，不会额外调用商家 API；当前没有可绘制的每日历史曲线。</p>
  </div>
</template>
<style scoped>
.source-note,.note,.detail,.subtitle { color:var(--muted,#687887); font-size:13px; line-height:1.7 }
.meter-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:22px; margin:20px 0 }
.meter-card { border:1px solid var(--border,#dfe6ed); border-radius:14px; padding:24px; background:linear-gradient(145deg,#f7faff,#fff) }
header { display:flex; align-items:center; justify-content:space-between; gap:12px } h3 { margin:0; font-size:20px }
.quality { font-size:12px; padding:4px 9px; border-radius:6px; background:#edf2ff; color:#3155df }
.quality.stale,.quality.error { color:#a66c0b; background:#fff5df }.quality.missing { color:#687887; background:#eff2f5 }
.main-value { display:block; font-size:34px; margin:20px 0 }.values { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin:20px 0 }
dt { font-size:13px; color:var(--muted,#687887) } dd { margin:6px 0 0; font-size:21px; font-weight:600 }
.transfer,.billing-track { height:12px; border-radius:8px; overflow:hidden; background:#e6ebf4 }.transfer { background:#139b87 }
.transfer span,.billing-track span { display:block; height:100%; background:#365ce8 }.billing-track span.old { background:#b48b43 }
.detail { margin:5px 0 }.note { margin:14px 0 0 }.warning { color:#a66c0b }
@media(max-width:720px) { .meter-grid { grid-template-columns:1fr }.meter-card { padding:18px } }
</style>
