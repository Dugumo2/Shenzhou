<script setup lang="ts">
import { computed } from 'vue'
import type { Service } from '../types'
import { formatGB, formatDate, serviceUsage } from '../display'
const props = defineProps<{ service: Service; lifecycleOnly?: boolean }>()
const usage = computed(() => serviceUsage(props.service))
</script>
<template><dl class="metrics-grid">
  <div v-if="!lifecycleOnly"><dt>本期总额度</dt><dd>{{ formatGB(service.quota_bytes) }}</dd></div>
  <div v-if="!lifecycleOnly"><dt>{{ usage.label }}</dt><dd>{{ formatGB(usage.bytes, '暂无可靠统计') }}</dd></div>
  <div v-if="!lifecycleOnly"><dt>剩余额度</dt><dd>{{ formatGB(service.remaining_bytes, '待核算') }}</dd></div>
  <div><dt>下次流量重置</dt><dd class="date-value">{{ formatDate(service.next_reset_at) }}</dd></div>
  <div><dt>到期时间</dt><dd class="date-value">{{ formatDate(service.expires_at) }}</dd></div>
  <div><dt>服务状态</dt><dd class="date-value">{{ service.status_label }}</dd></div>
</dl></template>
