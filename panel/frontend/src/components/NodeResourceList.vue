<script setup lang="ts">
import type { NodeResource } from '../inventoryTypes'
import NodeResourceDetails from './NodeResourceDetails.vue'
defineProps<{ nodes: NodeResource[] }>()
</script>
<template>
  <section class="node-resource-list" aria-label="节点与外部资源">
    <details v-for="node in nodes" :key="node.id" class="node-entry">
      <summary><strong>{{ node.name }}</strong><span>{{ node.external_kind === 'subscription' ? '上游订阅来源' : node.kind === 'external' ? '外部节点' : node.kind === 'unknown' ? '归属待核实' : '自有资源' }} · {{ node.protocol_label }}</span></summary>
      <NodeResourceDetails :node="node" />
    </details>
    <p v-if="!nodes.length" role="status">尚无已核实的节点资料，请先完成资源盘点并关联实际核心与端点。</p>
  </section>
</template>
<style scoped>
.node-resource-list { display:grid; gap:14px; min-width:0; }.node-entry { min-width:0; border:1px solid var(--border,#dfe6ed); border-radius:12px; overflow:hidden; }
summary { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; padding:16px; cursor:pointer; background:var(--surface,#fff); } summary strong { overflow-wrap:anywhere; } summary span { font-size:12px; color:var(--muted,#687887); } summary:focus-visible { outline:2px solid #365ce8; outline-offset:-3px; }
p { color:var(--muted,#687887); line-height:1.7; }
</style>
