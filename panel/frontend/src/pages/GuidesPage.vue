<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { request, errorMessage } from '../api'
import type { Client, Guide } from '../types'
const route = useRoute()
const clients = ref<Client[]>([]), guide = ref<Guide | null>(null)
const busy = ref(true), guideBusy = ref(false), error = ref(''), guideError = ref(''), search = ref(''), selected = ref('')
const filtered = computed(() => clients.value.filter(c => (c.name + ' ' + c.os + ' ' + c.reason).toLocaleLowerCase().includes(search.value.toLocaleLowerCase())))
let generation = 0
async function open(id: string) {
  const current = ++generation
  selected.value = id; guide.value = null; guideError.value = ''; guideBusy.value = true
  try { const data = await request<Guide>('/catalog/clients/' + encodeURIComponent(id) + '/guide'); if (current === generation) guide.value = data }
  catch (e) { if (current === generation) guideError.value = errorMessage(e) }
  finally { if (current === generation) guideBusy.value = false }
}
async function load() {
  busy.value = true; error.value = ''
  try { clients.value = (await request<{ items: Client[] }>('/catalog/clients')).items; if (typeof route.query.client === 'string' && clients.value.some(c => c.id === route.query.client)) await open(route.query.client) }
  catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
onMounted(load)
watch(() => route.query.client, id => { if (typeof id === 'string' && clients.value.some(c => c.id === id)) void open(id) })
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">按软件查找</p><h1>使用指南</h1><p class="muted">了解首次导入、更新和应用的步骤。</p></div><el-button :loading="busy" @click="load">刷新</el-button></div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
  <div class="guide-layout"><aside class="surface guide-list"><label class="visually-hidden" for="guide-search">搜索软件或系统</label><el-input id="guide-search" v-model="search" clearable placeholder="搜索软件或系统" /><el-skeleton v-if="busy" :rows="4" animated /><button v-for="client in filtered" :key="client.id" :class="{ selected: selected === client.id }" @click="open(client.id)"><strong>{{ client.name }}</strong><span>{{ client.os || '路由器' }} · {{ client.verification === 'verified' ? '已验证' : client.verification === 'unsupported' ? '暂不支持' : '待实机验证' }}</span></button><p v-if="!busy && !filtered.length" class="muted">没有匹配的指南。</p></aside>
    <section class="surface guide-content"><el-alert v-if="guideError" :title="guideError" type="error" :closable="false" show-icon /><el-skeleton v-if="guideBusy" :rows="6" animated /><template v-else-if="guide"><p class="eyebrow">软件指南</p><h2>{{ guide.title }}</h2><el-alert v-if="guide.verification !== 'verified'" title="这份指南尚未通过当前版本实机验证。" type="warning" :closable="false" show-icon /><p class="small muted">软件版本：{{ guide.software_version || '待核对' }} · 内置核心版本：{{ guide.core_version || '待核对' }}</p><ol class="guide-steps"><li v-for="step in guide.steps" :key="step.title"><h3>{{ step.title }}</h3><p>{{ step.body }}</p></li></ol><div class="inline-note"><h3>使用前请留意</h3><ul><li v-for="limit in guide.limitations" :key="limit">{{ limit }}</li></ul></div><div class="release-facts"><span>资源发布：{{ guide.update_status.published === 'unknown' ? '待确认' : guide.update_status.published }}</span><span>客户端下载：{{ guide.update_status.downloaded === 'unknown' ? '待确认' : guide.update_status.downloaded }}</span><span>客户端应用：{{ guide.update_status.applied === 'unknown' ? '待确认' : guide.update_status.applied }}</span></div></template><div v-else-if="!guideError && !guideBusy" class="empty-state"><h2>选择一份指南</h2><p>从左侧选择你正在使用的软件。</p></div></section>
  </div>
</template>
