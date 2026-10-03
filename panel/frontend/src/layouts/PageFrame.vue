<script setup lang="ts">
import type { PageTemplate } from '../navigation'
type PageState = 'ready' | 'loading' | 'empty' | 'error' | 'stale' | 'not_connected' | 'saving' | 'unknown_result'
withDefaults(defineProps<{ kind?: PageTemplate; title?: string; description?: string; state?: PageState; message?: string; retryable?: boolean }>(), { kind: 'list', title: '', description: '', state: 'ready', message: '', retryable: false })
defineEmits<{ retry: [] }>()
const stateLabels: Record<PageState, string> = { ready: '', loading: '正在加载…', empty: '暂无内容', error: '读取失败', stale: '资料已过期', not_connected: '尚未接入', saving: '正在保存…', unknown_result: '请求结果尚未确认' }
</script>
<template>
  <div class="page-frame" :class="'page-frame-' + kind" :data-page-template="kind">
    <header v-if="title || $slots.heading || $slots.actions" class="page-frame-heading">
      <div><slot name="heading"><h1>{{ title }}</h1><p v-if="description" class="muted">{{ description }}</p></slot></div>
      <div v-if="$slots.actions" class="page-frame-actions"><slot name="actions" /></div>
    </header>
    <div v-if="$slots.steps" class="page-frame-steps"><slot name="steps" /></div>
    <div v-if="$slots.summary" class="page-frame-summary"><slot name="summary" /></div>
    <div v-if="$slots.tabs" class="page-frame-tabs"><slot name="tabs" /></div>
    <div v-if="$slots.filters" class="page-frame-filters"><slot name="filters" /></div>
    <slot name="state"><div v-if="state !== 'ready'" class="page-frame-state" :class="'page-frame-state-' + state" :role="state === 'error' ? 'alert' : 'status'" :aria-busy="state === 'loading' || state === 'saving' ? true : undefined"><div><strong>{{ stateLabels[state] }}</strong><p v-if="message">{{ message }}</p></div><button v-if="retryable && state !== 'loading' && state !== 'saving'" type="button" @click="$emit('retry')">{{ state === 'unknown_result' ? '使用原请求重试' : '重试' }}</button></div></slot>
    <slot />
    <div v-if="$slots.preview" class="page-frame-preview"><slot name="preview" /></div>
    <footer v-if="$slots.footer" class="page-frame-footer"><slot name="footer" /></footer>
  </div>
</template>
