<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { request, errorMessage, ApiError } from '../api'
import { boundedRequest } from '../billingPending'
import type { CandidateDelivery } from '../delivery-types'

const props = defineProps<{ serviceId: string; clientId: string }>()
const delivery = ref<CandidateDelivery | null>(null)
const loading = ref(false), obtaining = ref(false), error = ref(''), copied = ref('')
const revisionReady = ref(false)
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
const canObtain = computed(() => supported.value && revisionReady.value && !!delivery.value
  && delivery.value.state !== 'blocked' && !obtaining.value && !loading.value)

onBeforeUnmount(() => { disposed = true; generation++; pending.value = null })

async function load() {
  if (disposed) return
  const key = JSON.stringify([props.serviceId, props.clientId])
  if (key !== contextKey) {
    contextKey = key
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
  <section aria-label="交付资源" class="delivery-resources">
    <el-alert title="本机合成演示资源" description="仅验证获取、复制和实际下载。演示节点不可连接；地址需要本人登录，不能用于客户端无人值守自动更新。" type="warning" :closable="false" show-icon />
    <p v-if="!supported" class="muted">这个软件尚未提供资源编译，不提供下载地址。</p>
    <el-skeleton v-else-if="loading && !delivery" :rows="2" animated />
    <template v-else>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="spaced" />
      <template v-if="delivery">
        <p class="small muted">{{ delivery.message }}</p>
        <el-alert v-if="delivery.update_error" :title="delivery.update_error" type="warning" :closable="false" show-icon class="spaced" />
        <p v-if="delivery.updates_available" class="small">候选规则已变化。重新获取会生成新内容并保持原下载地址。</p>
        <el-button v-if="delivery.state !== 'blocked'" type="primary" :loading="obtaining" :disabled="!canObtain" @click="obtain">{{ pending && !obtaining ? '重试确认获取结果' : delivery.state === 'ready' ? '重新获取演示资源' : '获取演示资源' }}</el-button>
        <p v-if="pending && !obtaining" class="small muted">上次获取结果尚未确认，重试会沿用同一操作标识；此前资源保留。</p>
        <p v-if="resources.length" class="small muted">这些资源来自同一份候选自建规则；软件切换沿用这份服务，不增加额度或接入身份。</p>
        <ul v-if="resources.length" class="resource-list">
          <li v-for="resource in resources" :key="resource.key">
            <div><strong>{{ resource.label }}</strong><span class="small muted">{{ resource.filename }} · {{ resource.bytes }} 字节</span></div>
            <div class="resource-actions"><el-button tag="a" :href="resource.download_url" download>下载</el-button><el-button @click="copyAddress(resource.download_url, resource.key)">{{ copied === resource.key ? '地址已复制' : '复制地址' }}</el-button></div>
          </li>
        </ul>
        <p class="small muted">{{ delivery.unsupported_resources.join('、') }}尚未编译。真实订阅令牌、旧地址兼容和客户端应用仍待验证。</p>
      </template>
    </template>
    <el-button v-if="supported" :loading="loading" :disabled="loading || obtaining" @click="load">重新读取交付状态</el-button>
  </section>
</template>

<style scoped>
.delivery-resources { margin: 16px 0; }
.resource-list { padding: 0; list-style: none; }
.resource-list li { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--el-border-color-light); }
.resource-list li span { display: block; margin-top: 4px; }
.resource-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.resource-actions .el-button + .el-button { margin-left: 0; }
</style>
