<script setup lang="ts">
import { computed } from 'vue'
import { formatDate } from '../display'
import type { ServerInventory } from '../inventoryTypes'
import { effectiveFreshness } from '../inventoryStatus'
const props = defineProps<{ server: ServerInventory; now: number; compact?: boolean }>()
defineEmits<{ open: [section: 'overview' | 'cores' | 'nodes'] }>()
const freshness = computed(() => effectiveFreshness(props.server.observation?.freshness ?? props.server.monitoring.state, props.server.observation?.expires_at ?? props.server.monitoring.expires_at, props.now))
const state = computed(() => freshness.value === 'fresh' ? props.server.monitoring.online === true ? '采样时在线' : props.server.monitoring.online === false ? '采样时离线' : '在线状态未知' : freshness.value === 'stale' ? '采样已过期' : freshness.value === 'target_changed' ? '目标已变更' : '尚无有效采样')
function percent(value: number | null | undefined) { return value == null || !Number.isFinite(value) ? '未知' : `${value.toLocaleString('zh-CN', { maximumFractionDigits: 1 })}%` }
function rate(value: number | null | undefined) {
  if (freshness.value !== 'fresh' || value == null || !Number.isFinite(value) || value < 0) return '未知'
  const unit = value >= 1_000_000 ? 'MB/s' : value >= 1_000 ? 'KB/s' : 'B/s'
  return `${(value / (unit === 'MB/s' ? 1_000_000 : unit === 'KB/s' ? 1_000 : 1)).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} ${unit}`
}
</script>
<template>
  <article class="server-card" :class="{ compact }" :aria-label="server.name">
    <header><div><button class="server-name" @click="$emit('open', 'overview')">{{ server.name }}</button><p class="small muted">{{ server.adapter === 'unconfigured' ? '适配器未配置' : server.adapter }} · {{ server.enabled ? '登记启用' : '登记停用' }}</p></div><span class="state" :class="{ stale: freshness === 'stale' || freshness === 'target_changed' }">{{ state }}</span></header>
    <dl class="metrics"><div><dt>CPU</dt><dd>{{ percent(server.observation?.metrics.cpu_percent) }}</dd></div><div><dt>内存</dt><dd>{{ percent(server.observation?.metrics.memory_percent) }}</dd></div><div><dt>磁盘</dt><dd>{{ percent(server.observation?.metrics.disk_percent) }}</dd></div><div><dt>接收速率</dt><dd>{{ rate(server.observation?.metrics.network_rx_bytes_per_second) }}</dd></div><div><dt>发送速率</dt><dd>{{ rate(server.observation?.metrics.network_tx_bytes_per_second) }}</dd></div></dl>
    <div class="sample small muted"><span>最近观测：{{ server.observation?.observed_at ? formatDate(server.observation.observed_at) : '暂无可信样本' }}</span><span>来源：{{ server.observation?.source || server.monitoring.source || '尚未接入' }}</span><span v-if="server.last_seen_at">登记联系：{{ formatDate(server.last_seen_at) }}</span></div>
    <footer><button @click="$emit('open', 'cores')">核心 {{ server.core_count ?? '未知' }} <span aria-hidden="true">→</span></button><button @click="$emit('open', 'nodes')">节点 {{ server.node_count ?? '未知' }} <span aria-hidden="true">→</span></button><button @click="$emit('open', 'overview')">查看详情 <span aria-hidden="true">→</span></button></footer>
  </article>
</template>
<style scoped>
.server-card{min-width:0;padding:22px;border:1px solid var(--sz-border,#e6ebf3);border-radius:16px;background:var(--sz-surface,#fff);box-shadow:0 5px 20px #20386205}.server-card header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}.server-name{font-size:19px;font-weight:650;text-align:left;overflow-wrap:anywhere}.server-card button{border:0;padding:0;background:none;color:var(--el-color-primary,#365ccb);cursor:pointer;font-family:inherit;min-height:32px}.server-card button:focus-visible{outline:2px solid currentColor;outline-offset:4px}.server-card header p{margin:4px 0 0}.state{padding:5px 8px;border-radius:6px;background:#eef3fa;color:#53647c;font-size:12px;white-space:nowrap}.state.stale{color:#92611d;background:#fff5e4}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px 12px;margin:24px 0}.metrics dt{font-size:12px;color:#75849b}.metrics dd{margin:6px 0 0;font-size:18px;font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.sample{display:flex;flex-direction:column;gap:5px;overflow-wrap:anywhere}footer{display:flex;flex-wrap:wrap;gap:18px;margin-top:18px;border-top:1px solid #edf0f5;padding-top:12px}footer button{font-size:13px}.compact .metrics{grid-template-columns:repeat(5,minmax(0,1fr));margin:16px 0}.compact .sample{flex-direction:row;flex-wrap:wrap;gap:8px 20px}.compact{padding:18px 22px}@media(max-width:520px){.server-card,.compact{padding:17px}.server-card header{flex-wrap:wrap}.metrics,.compact .metrics{grid-template-columns:repeat(3,minmax(0,1fr));gap:16px 10px}.metrics dd{font-size:16px}footer{gap:14px}.compact .sample{flex-direction:column}}
</style>
