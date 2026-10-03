<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { request, errorMessage, ApiError } from '../api'
import { boundedRequest } from '../billingPending'
import type { CandidateDelivery } from '../delivery-types'
import { connectionSteps } from '../connectionSteps'

const props = defineProps<{ serviceId: string; clientId: string }>()
const delivery = ref<CandidateDelivery | null>(null)
const loading = ref(false), obtaining = ref(false), error = ref(''), copied = ref('')
const revisionReady = ref(false)
const activeStep = ref(0)
let generation = 0
let contextKey = ''
let disposed = false
// 每个软件只保留一次未确认请求；同键重试不会创建设备身份。
const pending = ref<{ serviceId: string; clientId: string; revision: number; key: string } | null>(null)
const endpoint = computed(() => '/me/services/' + encodeURIComponent(props.serviceId) + '/delivery')
const resources = computed(() => (delivery.value?.resources || []).filter(item => {
  const prefix = '/api/v1/me/services/' + encodeURIComponent(props.serviceId) + '/delivery/' + encodeURIComponent(props.clientId) + '/resources/'
  return item.download_url.startsWith(prefix) && /^[a-z][a-z0-9-]{0,63}$/.test(item.download_url.slice(prefix.length))
}))
const supported = computed(() => ['windows', 'v2rayng', 'android'].includes(props.clientId))
const steps = computed(() => connectionSteps(props.clientId, resources.value))
const currentStep = computed(() => steps.value[activeStep.value])
const canObtain = computed(() => supported.value && revisionReady.value && !!delivery.value
  && delivery.value.state !== 'blocked' && !obtaining.value && !loading.value)

onBeforeUnmount(() => { disposed = true; generation++; pending.value = null })

async function load() {
  if (disposed) return
  const key = JSON.stringify([props.serviceId, props.clientId])
  if (key !== contextKey) {
    contextKey = key
    activeStep.value = 0
    delivery.value = null; pending.value = null; obtaining.value = false
  } else if (obtaining.value) return
  const current = ++generation
  error.value = ''; copied.value = ''; loading.value = true; revisionReady.value = false
  if (!supported.value) { loading.value = false; return }
  try {
    const value = await boundedRequest(request<CandidateDelivery>(endpoint.value + '?client_adapter=' + encodeURIComponent(props.clientId)))
    if (current === generation) { delivery.value = value; revisionReady.value = true }
  } catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) loading.value = false }
}

async function obtain() {
  if (disposed || !canObtain.value || !delivery.value) return
  const current = ++generation
  obtaining.value = true; error.value = ''; copied.value = ''
  const serviceId = props.serviceId, clientId = props.clientId, revision = delivery.value.service_revision
  if (!pending.value || pending.value.serviceId !== serviceId || pending.value.clientId !== clientId) {
    pending.value = { serviceId, clientId, revision, key: crypto.randomUUID() }
  }
  const body = { client_adapter: clientId, expected_revision: pending.value.revision, idempotency_key: pending.value.key }
  try {
    const value = await boundedRequest(request<CandidateDelivery>(endpoint.value, 'POST', body))
    if (current === generation) { delivery.value = value; pending.value = null; revisionReady.value = true }
  } catch (e) {
    if (current !== generation) return
    error.value = errorMessage(e)
    if (e instanceof ApiError && e.status === 409 && ['revision_conflict', 'idempotency_conflict', 'delivery_blocked'].includes(e.code)) {
      pending.value = null; revisionReady.value = false
      error.value += ' 请重新读取交付状态，再获取资源。'
      if (e.code === 'delivery_blocked') delivery.value = { ...delivery.value, state: 'blocked', download_url: null, resources: [] }
    } else if (e instanceof ApiError && e.status === 422) pending.value = null
  }
  finally { if (current === generation) obtaining.value = false }
}

async function copyAddress(url: string, key: string) {
  const current = generation
  try {
    await navigator.clipboard.writeText(new URL(url, window.location.origin).href)
    if (current === generation) copied.value = key
  } catch { if (current === generation) error.value = '浏览器未允许复制，请使用下载按钮获取资源。' }
}
watch(() => [props.serviceId, props.clientId], load, { immediate: true, flush: 'sync' })
</script>

<template>
  <section aria-label="软件连接步骤" class="delivery-resources">
    <el-alert title="当前为演示，真实订阅尚未接入" description="可预览和下载示例文件，但不能连接代理；演示地址需要登录，不能用于客户端自动更新。" type="warning" :closable="false" show-icon />
    <p v-if="!supported" class="muted">这个软件尚未提供资源编译，不提供下载地址。</p>
    <el-skeleton v-else-if="loading && !delivery" :rows="2" animated />
    <template v-else>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="spaced" />
      <template v-if="delivery">
        <p v-if="delivery.state === 'blocked'" class="quality-note">{{ delivery.message }}</p>
        <el-alert v-if="delivery.update_error" :title="delivery.update_error" type="warning" :closable="false" show-icon class="spaced" />
        <p v-if="delivery.updates_available" class="small">规则已更新，可重新生成演示文件。</p>
        <el-button v-if="delivery.state !== 'blocked' && (delivery.state !== 'ready' || pending || delivery.updates_available)" type="primary" :loading="obtaining" :disabled="!canObtain" @click="obtain">{{ pending && !obtaining ? '重试确认获取结果' : delivery.state === 'ready' ? '更新演示文件' : '准备演示文件' }}</el-button>
        <p v-if="pending && !obtaining" class="small muted">上次获取结果尚未确认，重试会沿用同一操作标识；此前资源保留。</p>
        <template v-if="delivery.state === 'ready'">
          <nav class="connection-steps" aria-label="连接步骤"><button v-for="(step, index) in steps" :key="step.title" type="button" :class="{active: activeStep === index}" :aria-current="activeStep === index ? 'step' : undefined" @click="activeStep = index">{{ index + 1 }}. {{ step.title }}</button></nav>
          <article v-if="currentStep" class="connection-step"><h3>{{ currentStep.title }}</h3><p class="muted">{{ currentStep.description }}</p>
            <div v-if="currentStep.resource" class="resource-actions"><el-button type="primary" tag="a" :href="currentStep.resource.download_url" download>{{ currentStep.downloadLabel }}</el-button><el-button @click="copyAddress(currentStep.resource.download_url, currentStep.resource.key)">{{ copied === currentStep.resource.key ? '地址已复制' : '复制演示下载地址' }}</el-button></div>
            <p v-else-if="activeStep < steps.length - 1" class="quality-note">这一步所需的文件尚未准备好，请刷新状态后重试。</p>
          </article>
          <div class="step-navigation"><el-button v-if="activeStep > 0" @click="activeStep--">上一步</el-button><el-button v-if="activeStep < steps.length - 1" @click="activeStep++">查看下一步</el-button></div>
        </template>
      </template>
    </template>
    <el-button v-if="supported" text :loading="loading" :disabled="loading || obtaining" @click="load">刷新状态</el-button>
  </section>
</template>

<style scoped>
.delivery-resources { margin: 16px 0; }
.connection-steps { display: flex; gap: 10px; flex-wrap: wrap; margin: 24px 0; }
.connection-steps button { font: inherit; font-size: 14px; cursor: pointer; background: transparent; border: 1px solid var(--border, #dfe6ed); border-radius: 8px; padding: 10px 16px; color: var(--muted, #687887); }
.connection-steps button.active { color: var(--el-color-primary); border-color: var(--el-color-primary); background: #eef3ff; }
.connection-step { padding: 4px 0 18px; }
.step-navigation { margin: 8px 0 18px; }
.resource-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.resource-actions .el-button + .el-button { margin-left: 0; }
</style>
