<script setup lang="ts">
import { computed, useId } from 'vue'
import { formatDate } from '../display'

const props = withDefaults(defineProps<{
  loading?: boolean
  disabled?: boolean
  error?: string
  sourceError?: string
  lastReadAt?: string | null
  sampledAt?: string | null
  label?: string
}>(), { loading: false, disabled: false, error: '', sourceError: '', lastReadAt: null, sampledAt: null, label: '更新数据' })
const emit = defineEmits<{ refresh: [] }>()
const statusId = useId()
const feedback = computed(() => props.loading ? '正在读取已采集数据…' : props.error ? (props.lastReadAt ? '读取失败，已保留上次内容' : '读取失败，请重试') : props.sourceError ? '统计来源暂不可更新，详见用量提示' : props.lastReadAt ? '已读取最新可用数据' : '读取已采集数据')
function refresh() { if (!props.loading && !props.disabled) emit('refresh') }
</script>

<template>
  <div class="refresh-control" :class="{ 'has-error': error }">
    <div class="refresh-feedback">
      <span :id="statusId" role="status" aria-live="polite" aria-atomic="true">{{ feedback }}</span>
      <span v-if="lastReadAt" class="refresh-time">页面读取：<time :datetime="lastReadAt">{{ formatDate(lastReadAt) }}</time></span>
      <span v-if="sampledAt" class="refresh-time">来源采样：<time :datetime="sampledAt">{{ formatDate(sampledAt) }}</time></span>
    </div>
    <el-button class="refresh-button" :disabled="disabled || loading" :aria-busy="loading" :aria-describedby="statusId" :aria-label="loading ? '正在读取已采集数据' : error ? '重试读取数据' : label" @click="refresh">
      <svg class="refresh-icon" :class="{ 'is-spinning': loading }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 7a7 7 0 0 1 11.55-1.65L20 8M4 16l2.35 2.65A7 7 0 0 0 17.9 17" /></svg>
      <span>{{ loading ? '正在更新' : error ? '重试读取' : label }}</span>
    </el-button>
  </div>
</template>

<style scoped>
.refresh-control { display: flex; align-items: center; justify-content: flex-end; gap: 12px; min-width: 0; max-width: 100%; }
.refresh-feedback { display: flex; flex-direction: column; gap: 3px; text-align: right; color: var(--sz-text-secondary, #63748f); font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.refresh-time { color: var(--sz-text-secondary, #63748f); font-size: 11px; }
.has-error .refresh-feedback > :first-child { color: var(--el-color-danger, #b53b3b); }
.refresh-button { flex-shrink: 0; min-height: 40px; border-color: var(--sz-border, #e6ebf3); border-radius: 9px; }
.refresh-icon { width: 16px; height: 16px; margin-right: 7px; flex-shrink: 0; }
.is-spinning { animation: refresh-spin 1.1s linear infinite; }
@keyframes refresh-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .is-spinning { animation: none; } }
@media (max-width: 640px) { .refresh-control { flex-wrap: wrap; justify-content: flex-start; gap: 8px; } .refresh-feedback { text-align: left; } .refresh-button { min-height: 44px; } }
</style>
