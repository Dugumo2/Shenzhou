<script setup lang="ts">
import { computed } from 'vue'
import type { LineInventory } from '../inventoryTypes'
import { formatDate } from '../display'
const props = defineProps<{ line: LineInventory; verification: string; selected?: boolean }>()
const exit = computed(() => props.line.path?.terminal_egress || props.line.egress)
defineEmits<{ open: [] }>()
</script>

<template>
  <article class="line-card" :class="{ selected }">
    <header><div><p class="eyebrow">已有线路登记</p><h2><button class="line-title" type="button" @click="$emit('open')">{{ line.name }}</button></h2><span class="small muted">#{{ line.id.slice(0, 8) }}</span></div><el-tag type="info">{{ line.enabled ? '登记启用' : '登记停用' }}</el-tag></header>
    <div class="card-path" aria-label="入口与出口登记摘要">
      <section><span class="path-label">入口 · {{ line.ingress_count }}</span><p v-for="entry in line.ingresses.slice(0, 2)" :key="entry.id"><strong>{{ entry.name }}</strong><span>{{ entry.server.name }} · {{ entry.protocol_label }}</span></p><p v-if="!line.ingresses.length" class="muted">入口资料未知</p><span v-if="line.ingress_count > 2" class="small muted">其余入口在详情查看</span></section>
      <span class="card-arrow" aria-hidden="true">→</span>
      <section><span class="path-label">{{ exit.ownership === 'external' ? '外部出口资源' : '出口资源' }}</span><p><strong>{{ exit.name }}</strong><span>{{ exit.protocol || exit.kind_label }} · 执行：{{ exit.server.name }}</span></p><p v-if="exit.id !== line.egress.id" class="small muted">经 {{ line.egress.name }} · 已有中间段</p></section>
    </div>
    <dl><div><dt>端点登记</dt><dd>{{ line.endpoint_registration === 'enabled' ? '登记均启用' : '含停用登记' }}</dd></div><div><dt>路径来源</dt><dd>{{ line.path?.source === 'inventory_registration' ? '现有资源登记' : line.path?.source || '暂无路径证据' }}</dd></div><div><dt>整链检测</dt><dd>{{ verification }}</dd></div><div><dt>最近检查</dt><dd>{{ line.verification.observed_at ? formatDate(line.verification.observed_at) : '尚无整链回执' }}</dd></div></dl>
    <footer><p class="small muted">登记关联不代表中间跳段或整链能力已证实。</p><el-button @click="$emit('open')">查看路径与详情</el-button></footer>
  </article>
</template>

<style scoped>
.line-card{border:1px solid var(--sz-border,#e6ebf3);border-radius:16px;padding:20px;background:var(--sz-surface,#fff);min-width:0}.line-card.selected{border-color:var(--el-color-primary,#4263eb)}header{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}h2{margin:4px 0;font-size:19px}.line-title{font:inherit;font-weight:700;color:inherit;padding:0;border:0;background:none;text-align:left;cursor:pointer;overflow-wrap:anywhere}.line-title:hover{color:var(--el-color-primary,#4263eb)}.card-path{display:grid;grid-template-columns:minmax(0,1fr) 22px minmax(0,1fr);align-items:start;gap:10px;background:var(--sz-bg,#f6f8fc);padding:16px;border-radius:12px;margin:20px 0}.card-path p{margin:10px 0 0;display:grid;gap:5px;overflow-wrap:anywhere}.card-path p span,.path-label{font-size:12px;color:var(--sz-text-secondary,#63748f)}.card-arrow{align-self:center;color:var(--sz-text-secondary,#63748f)}dl{display:grid;gap:12px;margin:0}dl div{display:flex;gap:12px;justify-content:space-between}dt{color:var(--sz-text-secondary,#63748f);font-size:13px;flex-shrink:0}dd{margin:0;font-size:13px;text-align:right;overflow-wrap:anywhere}footer{margin-top:18px;display:flex;align-items:center;justify-content:space-between;gap:12px}footer p{line-height:1.6;margin:0}.eyebrow{margin:0}@media(max-width:520px){.line-card{padding:16px}.card-path{grid-template-columns:1fr;gap:8px}.card-arrow{transform:rotate(90deg);justify-self:center}footer{align-items:flex-start;flex-direction:column}footer .el-button{width:100%;min-height:44px}}
</style>
