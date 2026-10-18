<script setup lang="ts">
import { computed } from 'vue'
import type { Service } from '../types'
import { formatGB, formatDate, serviceRemaining, serviceUsage } from '../display'
const props = defineProps<{ service: Service; lifecycleOnly?: boolean }>()
const usage = computed(() => serviceUsage(props.service))
</script>
<template><dl class="metrics-grid">
  <div v-if="!lifecycleOnly"><dt>本期总额度</dt><dd>{{ formatGB(service.quota_bytes) }}</dd></div>
  <div v-if="!lifecycleOnly"><dt>{{ usage.label }}</dt><dd>{{ formatGB(usage.bytes, '暂无可靠统计') }}</dd></div>
  <div v-if="!lifecycleOnly"><dt>剩余额度</dt><dd>{{ formatGB(serviceRemaining(service), '待核算') }}</dd></div>
  <div><dt>下次流量重置</dt><dd class="date-value">{{ formatDate(service.next_reset_at) }}</dd></div>
  <div><dt>到期时间</dt><dd class="date-value">{{ formatDate(service.expires_at) }}</dd></div>
  <div><dt>服务状态</dt><dd class="date-value">{{ service.service_status?.label || service.status_label }}</dd></div>
  <div class="service-number"><dt>服务编号</dt><dd class="date-value">{{ service.id }}</dd></div>
</dl></template>
<style scoped>
.metrics-grid > div { min-width: 0; }
.metrics-grid dd { overflow-wrap: anywhere; }
.service-number { grid-column: 1 / -1; }
</style>
