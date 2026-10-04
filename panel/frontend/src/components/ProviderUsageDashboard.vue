<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import ResourceUsageCard from './ResourceUsageCard.vue'
import type { ProviderUsage } from '../providerUsage'

const props = defineProps<{ data: ProviderUsage }>()
const now = ref(Date.now())
const meters = computed(() => props.data?.schema_version === 2 && Array.isArray(props.data.meters) ? props.data.meters : [])
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => { timer = setInterval(() => { now.value = Date.now() }, 10_000) })
onBeforeUnmount(() => { if (timer !== undefined) clearInterval(timer) })
</script>

<template>
  <div class="provider-usage" aria-label="资源用量">
    <p class="resource-scope-note">各资源分别统计，不合并为个人套餐用量。</p>
    <p v-if="data.error" class="resource-error" role="status">{{ data.error.message || '暂时无法读取资源用量，请稍后刷新。' }}</p>
    <div v-if="meters.length" class="meter-grid">
      <ResourceUsageCard v-for="meter in meters" :key="meter.id" :meter="meter" :now="now" />
    </div>
    <p v-else-if="!data.error" class="resource-empty">暂无资源用量记录。</p>
  </div>
</template>

<style scoped>
.resource-scope-note, .resource-empty, .resource-error { color: var(--muted, #687887); font-size: 13px; line-height: 1.7; }
.resource-error { color: #93631c; }
.meter-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 22px; margin: 20px 0; }
@media (max-width: 720px) { .meter-grid { grid-template-columns: 1fr; } }
</style>
