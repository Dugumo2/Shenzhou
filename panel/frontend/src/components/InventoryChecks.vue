<script setup lang="ts">
import { formatDate } from '../display'
import type { InventoryCheck } from '../inventoryTypes'
import { effectiveFreshness, useInventoryClock } from '../inventoryStatus'
defineProps<{ checks: { items: InventoryCheck[]; truncated: boolean } }>()
const now = useInventoryClock()
const states: Record<string,string> = { fresh:'有效期内', stale:'已过期', target_changed:'目标已变更', unknown:'未知' }
const results: Record<string,string> = { pass:'通过', fail:'失败', timeout:'超时', unknown:'未知' }
const kinds: Record<string,string> = { service_status:'服务状态', core_version:'核心版本', tcp:'TCP连接', udp:'UDP', tls:'TLS握手', dns:'DNS解析', egress:'出口', whole_path:'整链' }
const targets: Record<string,string> = { server:'服务器', core:'核心', ingress:'入口', egress:'出口', line:'线路' }
</script>
<template>
  <section class="inventory-checks"><h2>检测记录</h2><p class="small muted">按具体目标分别记录；单端点通过不代表整条线路可用。刷新页面不会发起检测。</p>
    <p v-if="!checks.items.length" class="muted">暂无可信检测回执。</p>
    <article v-for="check in checks.items" :key="check.id">
      <div><strong>{{ kinds[check.check_kind] || check.check_kind }}</strong> · {{ targets[check.target_kind] || check.target_kind }} #{{ check.target_id.slice(0,8) }} <el-tag type="info">{{ states[effectiveFreshness(check.freshness, check.expires_at, now)] }}</el-tag></div>
      <p>本次结果：{{ results[check.result] }} <span v-if="check.latency_ms !== null">· {{ check.latency_ms }} ms</span></p>
      <p class="small muted">来源 {{ check.source }} · 检测 {{ formatDate(check.observed_at) }} · 有效至 {{ formatDate(check.expires_at) }}</p>
      <p v-if="check.error_stage || check.error_code" class="small">失败阶段 {{ check.error_stage || '未提供' }} · {{ check.error_code || '未提供' }}</p>
    </article><p v-if="checks.truncated" class="small muted">仅显示最新100条回执。</p>
  </section>
</template>
<style scoped>.inventory-checks{margin:24px 0}.inventory-checks article{padding:14px 0;border-bottom:1px solid #e7ecf4}.inventory-checks h2{font-size:18px}.inventory-checks p{overflow-wrap:anywhere}</style>
