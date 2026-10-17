<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount } from 'vue'
import type { NodeResource } from '../inventoryTypes'
import ResourceUsageCard from './ResourceUsageCard.vue'

const props = defineProps<{ node: NodeResource }>()
const now = ref(Date.now())
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 10_000) })
onBeforeUnmount(() => { if (timer !== undefined) clearInterval(timer) })
const cycleLabel = computed(() => {
  const cycle = props.node.purchase_meter?.cycle
  if (!cycle) return '周期待接入'
  if (cycle.kind === 'fixed_days' && cycle.starts_at && cycle.ends_at) {
    const days = (Date.parse(cycle.ends_at) - Date.parse(cycle.starts_at)) / 86_400_000
    if (Number.isInteger(days) && days > 0) return `每 ${days} 天重置`
  }
  return ({ provider: '按供应商账期', calendar_month: '按自然月重置', manual: '手动周期', none: '无重置周期', fixed_days: '按固定天数重置' } as Record<string,string>)[cycle.kind] || '周期待核实'
})
</script>
<template>
  <section v-if="node.kind === 'external'" class="external-resource" aria-label="外部资源资料">
    <h4>{{ node.external_kind === 'subscription' ? '上游订阅来源' : '外部节点采购' }}</h4>
    <p v-if="node.external_kind === 'subscription'">来源更新与节点授权分别管理。新增来源节点须独立授权后才可供用户使用；用户输出订阅保留在服务详情。</p>
    <p>供应商负责上游服务器与核心维护；此处登记连接资源及采购记录。</p>
    <template v-if="node.purchase_meter">
      <p class="purchase-cycle">{{ cycleLabel }} · 采购账期独立于用户套餐</p>
      <ResourceUsageCard :meter="node.purchase_meter" :now="now" />
      <p class="purchase-note">采购用量不分摊给用户，续费或替换资源不会重置用户套餐用量与有效期。</p>
    </template>
    <p v-else role="status">{{ node.purchase_meter_id ? '采购统计暂不可读取，请检查已授权来源的采集记录。' : '尚未关联采购统计，请先核实资源与采购记录的对应关系。' }}</p>
  </section>
</template>
<style scoped>
.external-resource { border-top:1px solid var(--border,#dfe6ed); padding-top:12px; margin-top:16px; min-width:0; }
h4 { margin:0 0 10px; } p { font-size:13px; line-height:1.7; overflow-wrap:anywhere; color:var(--muted,#687887); }.purchase-cycle { color:var(--text,#213652); font-weight:600; }.purchase-note { margin-bottom:0; }
</style>
