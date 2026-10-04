<script setup lang="ts">
import { computed } from 'vue'
import type { Service } from '../types'
import { formatGB, formatDate } from '../display'
const props = defineProps<{ service: Service; lifecycleOnly?: boolean }>()
const usageLabel = computed(() => props.service.usage.quality === 'measured' ? '本期已用' : props.service.usage.quality === 'stale' ? '上次已确认用量' : '用量（待核算）')
const resourceSchedule = computed(() => props.service.source_type === 'p8' && !props.service.next_reset_at && !props.service.expires_at)
</script>
<template><dl class="metrics-grid">
  <div v-if="!lifecycleOnly"><dt>本期总额度</dt><dd>{{ formatGB(service.quota_bytes) }}</dd></div>
  <div v-if="!lifecycleOnly"><dt>{{ usageLabel }}</dt><dd>{{ formatGB(service.used_bytes, '暂无可靠统计') }}</dd></div>
  <div v-if="!lifecycleOnly"><dt>剩余额度</dt><dd>{{ formatGB(service.remaining_bytes, '待核算') }}</dd></div>
  <div v-if="resourceSchedule"><dt>有效期与流量重置</dt><dd class="date-value">按资源分别计算，详见用量卡片</dd></div>
  <template v-else><div><dt>下次流量重置</dt><dd class="date-value">{{ formatDate(service.next_reset_at) }}</dd></div>
  <div><dt>到期时间</dt><dd class="date-value">{{ formatDate(service.expires_at) }}</dd></div></template>
  <div><dt>服务状态</dt><dd class="date-value">{{ service.status_label }}</dd></div>
</dl></template>
