<script setup lang="ts">
import { computed } from 'vue'
import { formatDate, formatGB } from '../display'
import type { Observation } from '../inventoryTypes'
import { effectiveFreshness, useInventoryClock } from '../inventoryStatus'
const props = defineProps<{ observation: Observation | null }>()
const now = useInventoryClock()
const fresh = computed(() => props.observation && effectiveFreshness(props.observation.freshness, props.observation.expires_at, now.value) === 'fresh')
const states: Record<string,string> = { fresh:'观测有效期内', stale:'观测已过期', target_changed:'目标已变更，请重新核验', unknown:'未接入' }
function number(value: number | null | undefined, unit='') { return value == null || !Number.isFinite(value) ? '未知' : value.toLocaleString('zh-CN', { maximumFractionDigits: 1 }) + unit }
function rate(value: number | null | undefined) { return fresh.value ? number(value, ' B/s') : '未知' }
</script>
<template>
  <section aria-label="机器观测"><h2>状态与指标</h2>
    <p v-if="!observation" class="muted">未接入监控；暂无可信样本。需要先完成服务器映射并启用获批的固定主机采样，再读取观测数据。</p>
    <template v-else><el-tag type="info">{{ states[effectiveFreshness(observation.freshness, observation.expires_at, now)] || '状态未知' }}</el-tag>
      <p>采样时状态：{{ observation.metrics.online == null ? '未知' : observation.metrics.online ? '在线' : '离线' }}<span v-if="effectiveFreshness(observation.freshness, observation.expires_at, now) !== 'fresh'">；当前状态未知</span></p>
      <p class="small muted">来源 {{ observation.source }} · 采样 {{ formatDate(observation.observed_at) }} · 有效至 {{ formatDate(observation.expires_at) }}</p>
      <dl><div><dt>CPU</dt><dd>{{ number(observation.metrics.cpu_percent,'%') }} · 区间 {{ number(observation.metrics.cpu_window_seconds,'秒') }}</dd></div><div><dt>内存占用</dt><dd>{{ number(observation.metrics.memory_percent,'%') }}</dd></div><div><dt>磁盘占用</dt><dd>{{ number(observation.metrics.disk_percent,'%') }}</dd></div><div><dt>接收速率</dt><dd>{{ rate(observation.metrics.network_rx_bytes_per_second) }}</dd></div><div><dt>发送速率</dt><dd>{{ rate(observation.metrics.network_tx_bytes_per_second) }}</dd></div><div><dt>速率采样区间</dt><dd>{{ number(observation.metrics.network_window_seconds,'秒') }}</dd></div><div><dt>网卡接收累计</dt><dd>{{ formatGB(observation.metrics.network_rx_bytes, '未知') }}</dd></div><div><dt>网卡发送累计</dt><dd>{{ formatGB(observation.metrics.network_tx_bytes, '未知') }}</dd></div><div><dt>计数网卡</dt><dd>{{ observation.metrics.network_interfaces?.join('、') || '未知' }}</dd></div></dl>
      <p class="small muted">{{ observation.scope }} 网卡累计与收发速率是机器观测，不是个人套餐用量。</p>
      <p v-if="observation.metrics.network_rx_bytes_per_second == null || observation.metrics.network_tx_bytes_per_second == null" class="quality-note">速率暂不可确认；需要同一计数世代的相邻有效样本，重启或计数回退后重新建立基线。</p>
    </template>
  </section>
</template>
<style scoped>h2{font-size:18px}dl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}dt{font-size:13px;color:#75849b}dd{margin:6px 0;overflow-wrap:anywhere}.small{overflow-wrap:anywhere}@media(max-width:520px){dl{grid-template-columns:1fr}}</style>
