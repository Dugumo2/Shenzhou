<script setup lang="ts">
import { formatDate } from '../display'
import type { Observation } from '../inventoryTypes'
import { effectiveFreshness, useInventoryClock } from '../inventoryStatus'
defineProps<{ observation: Observation | null }>()
const now = useInventoryClock()
const states: Record<string,string> = { fresh:'有效期内的一次性观测', stale:'观测已过期', target_changed:'目标已变更，请重新核验', unknown:'未接入' }
function number(value: number | null | undefined, unit='') { return value == null ? '未知' : value.toLocaleString('zh-CN') + unit }
</script>
<template>
  <section aria-label="机器观测"><h2>状态与指标</h2>
    <p v-if="!observation" class="muted">未接入监控；暂无可信样本。</p>
    <template v-else><el-tag type="info">{{ states[effectiveFreshness(observation.freshness, observation.expires_at, now)] }}</el-tag>
      <p>采样时状态：{{ observation.metrics.online === null ? '未知' : observation.metrics.online ? '在线' : '离线' }}<span v-if="effectiveFreshness(observation.freshness, observation.expires_at, now) !== 'fresh'">；当前状态未知</span></p>
      <p class="small muted">来源 {{ observation.source }} · 采样 {{ formatDate(observation.observed_at) }} · 有效至 {{ formatDate(observation.expires_at) }}</p>
      <dl><div><dt>CPU</dt><dd>{{ number(observation.metrics.cpu_percent,'%') }} · 区间 {{ number(observation.metrics.cpu_window_seconds,'秒') }}</dd></div><div><dt>内存占用</dt><dd>{{ number(observation.metrics.memory_percent,'%') }}</dd></div><div><dt>磁盘占用</dt><dd>{{ number(observation.metrics.disk_percent,'%') }}</dd></div><div><dt>网卡接收累计</dt><dd>{{ number(observation.metrics.network_rx_bytes,' B') }}</dd></div><div><dt>网卡发送累计</dt><dd>{{ number(observation.metrics.network_tx_bytes,' B') }}</dd></div><div><dt>计数网卡</dt><dd>{{ observation.metrics.network_interfaces?.join('、') || '未知' }}</dd></div></dl>
      <p class="small muted">{{ observation.scope }} 未安装持续采集器时没有实时曲线。</p>
    </template>
  </section>
</template>
<style scoped>h2{font-size:18px}dl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}dt{font-size:13px;color:#75849b}dd{margin:6px 0;overflow-wrap:anywhere}@media(max-width:520px){dl{grid-template-columns:1fr}}</style>
