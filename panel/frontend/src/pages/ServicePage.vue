<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { request, errorMessage } from '../api'
import type { Client, Service } from '../types'
import ServiceMetrics from '../components/ServiceMetrics.vue'
import UsageOverview from '../components/UsageOverview.vue'
import DeliveryResources from '../components/DeliveryResources.vue'
import { auth } from '../auth'
const route = useRoute()
const service = ref<Service | null>(null), clients = ref<Client[]>([])
const busy = ref(true), error = ref(''), device = ref(''), os = ref(''), clientId = ref('')
const refreshKey = ref(0)
const activeSection = ref('overview'), choosing = ref(true)
const deviceOptions = [{ id: 'phone', label: '手机', hint: 'Android / iOS' }, { id: 'computer', label: '电脑', hint: 'Windows / macOS / Linux' }, { id: 'router', label: '路由器', hint: '查看适配情况' }]
const osOptions = computed(() => device.value === 'phone' ? ['Android', 'iOS'] : device.value === 'computer' ? ['Windows', 'macOS', 'Linux'] : [])
const matched = computed(() => clients.value.filter(c => c.device === device.value && (device.value === 'router' || c.os === os.value)))
const selected = computed(() => matched.value.find(c => c.id === clientId.value))
const delivery = computed(() => service.value?.clients?.find(c => c.id === clientId.value)?.delivery || service.value?.delivery)
// 只有后端明确返回下载地址时才提供下载动作；缺少地址时保持未交付状态，避免把提示文案当成成功。
const downloadUrl = computed(() => {
  const value = delivery.value?.download_url
  return typeof value === 'string' && value.trim() ? value : null
})
watch(device, () => { os.value = ''; clientId.value = '' })
watch(os, () => { clientId.value = '' })
watch(clientId, value => { choosing.value = !value })
let generation = 0
async function load() {
  const current = ++generation
  busy.value = true; error.value = ''
  try {
    const [detail, catalog] = await Promise.all([request<Service>('/me/services/' + encodeURIComponent(String(route.params.id))), request<{ items: Client[] }>('/catalog/clients')])
    if (current === generation) { service.value = detail; clients.value = catalog.items; refreshKey.value++ }
  } catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}
onMounted(load)
watch(() => route.params.id, () => { service.value = null; device.value = ''; activeSection.value = 'overview'; void load() })
</script>
<template>
  <RouterLink to="/services" class="back-link">← 我的服务</RouterLink>
  <div class="page-title"><div><p class="eyebrow">服务详情</p><h1>神舟云 <span class="short-id">#{{ String(route.params.id).slice(0, 8) }}</span></h1></div><el-button :loading="busy" @click="load">刷新</el-button></div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
  <el-skeleton v-if="busy && !service" :rows="6" animated />
  <template v-if="service">
    <el-tabs v-model="activeSection" class="service-sections" aria-label="服务功能分区">
    <el-tab-pane label="服务概览" name="overview">
    <section class="surface detail-summary"><ServiceMetrics :service="service" /><p v-if="service.usage.quality !== 'measured'" class="quality-note">{{ service.usage.message || '用量暂不可确认，剩余额度待核算。' }}</p></section>
    <div class="service-actions"><button class="surface" @click="activeSection = 'connect'"><strong>连接设置 →</strong><span>选择设备和软件，按步骤导入订阅。</span></button><button class="surface" @click="activeSection = 'usage'"><strong>查看流量用量 →</strong><span>查看本期使用情况和统计详情。</span></button></div>
    </el-tab-pane>
    <el-tab-pane label="流量用量" name="usage" lazy>
    <UsageOverview :key="service.id" :service-id="service.id" :refresh-key="refreshKey" />
    </el-tab-pane>
    <el-tab-pane label="连接设置" name="connect" lazy>
    <section class="surface setup-panel"><div class="section-title"><div><h2>{{ selected && !choosing ? selected.name + ' 连接设置' : '选择设备和软件' }}</h2><p class="muted small">更换软件沿用这份服务，无需重新选择线路。</p></div><el-button v-if="selected && !choosing" @click="choosing = true">更换设备或软件</el-button></div>
      <template v-if="choosing || !selected">
      <h3><span class="step-number">1</span>你准备在哪种设备上使用？</h3>
      <div class="device-options"><button v-for="option in deviceOptions" :key="option.id" type="button" :class="{ selected: device === option.id }" :aria-pressed="device === option.id" @click="device = option.id"><strong>{{ option.label }}</strong><span>{{ option.hint }}</span></button></div>
      <template v-if="device && device !== 'router'"><h3><span class="step-number">2</span>选择系统</h3><el-radio-group v-model="os" aria-label="系统"><el-radio-button v-for="system in osOptions" :key="system" :value="system">{{ system }}</el-radio-button></el-radio-group></template>
      <template v-if="device === 'router'"><el-alert title="路由器暂未提供经过验证的适配。" description="请选择电脑或手机查看现有软件指南。这里不会生成未经验证的链接。" type="info" :closable="false" show-icon class="spaced" /></template>
      <template v-else-if="os"><h3><span class="step-number">3</span>选择软件</h3><el-radio-group v-if="matched.length" v-model="clientId" aria-label="软件"><el-radio-button v-for="client in matched" :key="client.id" :value="client.id">{{ client.name }}</el-radio-button></el-radio-group><p v-else class="inline-note">这个系统暂未提供经过验证的适配。请选择其他系统查看现有软件指南。</p></template>
      <el-button v-if="selected" class="spaced" @click="choosing = false">继续使用 {{ selected.name }}</el-button>
      </template>
      <div v-if="selected" v-show="!choosing" class="software-result">
        <DeliveryResources v-if="auth.session?.environment?.is_demo" :key="service.id + ':' + selected.id" :service-id="service.id" :client-id="selected.id" />
        <template v-else-if="downloadUrl">
          <el-button tag="a" :href="downloadUrl" target="_blank" rel="noopener">下载资源</el-button>
          <p class="small muted">下载完成后仍需在软件中导入并应用；下载成功不代表客户端已生效。</p>
        </template>
        <el-alert v-else :title="'资源尚未提供可用下载链接。' + (delivery?.message ? ' ' + delivery.message : '')" type="info" show-icon :closable="false" />
        <RouterLink :to="{ path: '/guides', query: { client: selected.id } }" class="text-action">查看 {{ selected.name }} 使用指南 →</RouterLink>
      </div>
    </section>
    </el-tab-pane>
    </el-tabs>
  </template>
</template>
<style scoped>
.service-sections { margin-top: 12px; }
.service-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.service-actions button { padding: 24px; text-align: left; border: 1px solid var(--border, #e0e7ef); cursor: pointer; font: inherit; color: inherit; }
.service-actions strong, .service-actions span { display: block; }
.service-actions strong { color: var(--el-color-primary); font-size: 18px; margin-bottom: 9px; }
.service-actions span { color: var(--muted, #687887); font-size: 14px; }
.service-actions button:hover { border-color: var(--el-color-primary); }
@media(max-width: 640px) { .service-actions { grid-template-columns: 1fr; } }
</style>
