<script setup lang="ts">
import type { NodeResource } from '../inventoryTypes'
import { formatDate } from '../display'
import ExternalResourceDetails from './ExternalResourceDetails.vue'

defineProps<{ node: NodeResource }>()
const ownershipLabels = { owned: '自有资源', external: '外部资源', unknown: '所有权待核实' }
const coreLabels = { unknown: '核心归属待核实', conflict: '核心关联冲突', linked: '已登记核心关联' }
const ownerLabels = { unknown: '配置所有者待核实', observed_only: '仅有观测证据', verified: '配置所有者已核实' }
</script>
<template>
  <section class="node-resource-details" :aria-label="node.name + '资源详情'">
    <header><h3>{{ node.name }}</h3><span class="resource-kind">{{ ownershipLabels[node.kind] }}</span></header>
    <dl class="node-fields">
      <div><dt>资源类型</dt><dd>{{ node.external_kind === 'subscription' ? '上游订阅来源' : node.resource_type === 'ingress' ? '接入节点' : node.kind === 'external' ? '外部节点' : '出站资源' }}</dd></div>
      <div><dt>协议</dt><dd>{{ node.protocol_label }}</dd></div>
      <div><dt>{{ node.kind === 'external' ? '执行服务器' : '关联服务器' }}</dt><dd>{{ node.execution_server.name }}</dd></div>
      <div><dt>登记状态</dt><dd>{{ node.enabled === null ? '不适用' : node.enabled ? '已启用' : '已停用' }}</dd></div>
      <div><dt>端点地址</dt><dd>{{ node.endpoint.host || '待核实' }}</dd></div>
      <div><dt>端口</dt><dd>{{ node.endpoint.port ?? '待核实' }}</dd></div>
      <div><dt>资料版本</dt><dd>{{ node.revision }}</dd></div>
      <div v-if="node.next_resource"><dt>后续资源</dt><dd>{{ node.next_resource.name }}</dd></div>
      <div><dt>证据时间</dt><dd>{{ node.evidence.observed_at ? formatDate(node.evidence.observed_at) : '暂无记录' }}</dd></div>
      <div v-if="node.evidence.source"><dt>证据来源</dt><dd>{{ node.evidence.source }}</dd></div>
    </dl>
    <div class="core-references">
      <h4>{{ coreLabels[node.core_refs.state] }}</h4>
      <ul v-if="node.core_refs.items.length"><li v-for="core in node.core_refs.items" :key="core.id">{{ core.name }} · {{ core.core_type }} · {{ ownerLabels[core.owner_state] }}</li></ul>
      <p v-else>请先通过资源盘点核对端点与核心实例的对应关系。</p>
      <p v-if="node.kind === 'external' && node.core_refs.items.length">此处核心负责我方连接外部资源，不代表拥有供应商的服务器或核心。</p>
    </div>
    <ExternalResourceDetails v-if="node.kind === 'external'" :node="node" />
    <ul class="resource-boundaries"><li v-for="text in node.limitations" :key="text">{{ text }}</li></ul>
  </section>
</template>
<style scoped>
.node-resource-details { min-width:0; padding:18px; border:1px solid var(--border,#dfe6ed); border-radius:12px; background:var(--surface,#fff); }
header { display:flex; justify-content:space-between; flex-wrap:wrap; gap:12px; align-items:center; } h3 { margin:0; overflow-wrap:anywhere; }
.resource-kind { padding:4px 8px; border-radius:6px; font-size:12px; background:#edf2ff; color:#3155df; }
.node-fields { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; margin:20px 0; } dt { font-size:12px; color:var(--muted,#687887); } dd { margin:5px 0 0; overflow-wrap:anywhere; }
.core-references,.resource-boundaries { font-size:13px; line-height:1.7; overflow-wrap:anywhere; } .resource-boundaries { color:var(--muted,#687887); padding-left:18px; }
@media(max-width:640px) { .node-fields { grid-template-columns:1fr; }.node-resource-details { padding:14px; } }
</style>
