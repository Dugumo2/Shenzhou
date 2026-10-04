<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { request, errorMessage } from '../api'
import { boundedRequest } from '../billingPending'
import { p8ConnectionSteps, verifiedP8Resources } from '../p8Delivery'
import type { P8Delivery, P8Resource } from '../p8Delivery'

const props = defineProps<{ serviceId: string; clientId: string; refreshKey?: number }>()
const delivery = ref<P8Delivery | null>(null)
const loading = ref(false), error = ref(''), copied = ref(''), activeStep = ref(0)
const steps = computed(() => p8ConnectionSteps(props.clientId))
const resources = computed(() => verifiedP8Resources(delivery.value, props.serviceId, props.clientId))
const currentStep = computed(() => steps.value[activeStep.value])
const currentResources = computed(() => resources.value.filter(item => currentStep.value?.keys.includes(item.key)))
let generation = 0
let disposed = false

async function load() {
  if (disposed) return
  const current = ++generation
  delivery.value = null; error.value = ''; copied.value = ''; loading.value = true
  if (!steps.value.length) { loading.value = false; return }
  try {
    const value = await boundedRequest(request<P8Delivery>('/me/services/' + encodeURIComponent(props.serviceId) + '/p8-delivery?client_adapter=' + encodeURIComponent(props.clientId)))
    if (current !== generation) return
    if (value.service_id !== props.serviceId || value.client_id !== props.clientId) {
      error.value = '资源信息与当前服务不一致，请刷新后重试。'; return
    }
    delivery.value = value
  } catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) loading.value = false }
}

async function copyAddress(resource: P8Resource) {
  if (!resources.value.includes(resource)) return
  const current = generation
  try {
    await navigator.clipboard.writeText(resource.download_url)
    if (current === generation) copied.value = resource.key
  } catch { if (current === generation) error.value = '浏览器未允许复制，请使用下载按钮，或允许剪贴板权限后重试。' }
}
watch(() => [props.serviceId, props.clientId], () => { activeStep.value = 0; void load() }, { immediate: true, flush: 'sync' })
watch(() => props.refreshKey, () => { void load() })
onBeforeUnmount(() => { disposed = true; generation++; delivery.value = null })
</script>

<template>
  <section aria-label="软件连接步骤" class="p8-delivery-resources" :aria-busy="loading">
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
    <el-skeleton v-if="loading" :rows="2" animated />
    <p v-else-if="!steps.length" class="quality-note">这个软件暂未提供经过验证的适配。</p>
    <template v-else-if="delivery">
      <el-alert v-if="!resources.length" :title="delivery.state === 'available' ? '这份软件所需的资源尚未完整核验，请联系管理员核对。' : delivery.message" type="warning" :closable="false" show-icon />
      <template v-else>
        <p class="quality-note">资源响应与格式已核对；客户端导入、应用和实际连接尚未验收。</p>
        <nav class="connection-steps" aria-label="连接步骤"><button v-for="(step, index) in steps" :key="step.title" type="button" :class="{active: activeStep === index}" :aria-current="activeStep === index ? 'step' : undefined" @click="activeStep = index">{{ index + 1 }}. {{ step.title }}</button></nav>
        <article v-if="currentStep" class="connection-step"><h3>{{ currentStep.title }}</h3><p class="muted">{{ currentStep.description }}</p>
          <div v-for="resource in currentResources" :key="resource.key" class="resource-actions"><span>{{ resource.label }}</span><el-button type="primary" @click="copyAddress(resource)">{{ copied === resource.key ? '地址已复制' : '复制地址' }}</el-button><el-button tag="a" :href="resource.download_url" target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer">下载文件</el-button></div>
        </article>
        <div class="step-navigation"><el-button v-if="activeStep > 0" @click="activeStep--">上一步</el-button><el-button v-if="activeStep < steps.length - 1" @click="activeStep++">查看下一步</el-button></div>
      </template>
    </template>
    <el-button v-if="steps.length" text :loading="loading" :disabled="loading" @click="load">刷新资源状态</el-button>
  </section>
</template>

<style scoped>
.p8-delivery-resources { margin: 16px 0; }
.connection-steps { display: flex; gap: 10px; flex-wrap: wrap; margin: 24px 0; }
.connection-steps button { font: inherit; font-size: 14px; cursor: pointer; background: transparent; border: 1px solid var(--border, #dfe6ed); border-radius: 8px; padding: 10px 16px; color: var(--muted, #687887); }
.connection-steps button.active { color: var(--el-color-primary); border-color: var(--el-color-primary); background: #eef3ff; }
.connection-step { padding: 4px 0 18px; }
.step-navigation { margin: 8px 0 18px; }
.resource-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
.resource-actions .el-button + .el-button { margin-left: 0; }
</style>
