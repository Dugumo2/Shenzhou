<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { request, errorMessage } from '../api'
import type { GuideCatalog } from '../guide-types'
const route = useRoute()
const catalog = ref<GuideCatalog | null>(null), search = ref(''), client = ref('all'), selected = ref('')
const busy = ref(false), error = ref('')
const article = computed(() => catalog.value?.articles.find(item => item.id === selected.value))
let generation = 0
async function load() {
  const current = ++generation
  busy.value = true; error.value = ''
  try {
    const data = await request<GuideCatalog>('/catalog/guides?' + new URLSearchParams({ q: search.value, client: client.value }))
    if (current !== generation) return
    catalog.value = data
    if (!data.articles.some(item => item.id === selected.value)) selected.value = data.articles[0]?.id || ''
  } catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}
watch(() => route.query.client, value => {
  client.value = typeof value === 'string' ? value : 'all'
  selected.value = ({ windows: 'windows-v2rayn', v2rayng: 'android-v2rayng', android: 'android-sfa', router: 'router-readiness' } as Record<string,string>)[client.value] || ''
  void load()
}, { immediate: true })
onUnmounted(() => { generation++ })
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">帮助中心</p><h1>你想完成哪一步？</h1><p class="muted">按软件选择，或搜索导入、更新规则、流量等问题。</p></div><el-button :loading="busy" @click="load">刷新</el-button></div>
  <form class="table-filters" @submit.prevent="load"><label class="visually-hidden" for="guide-search">搜索指南全文</label><el-input id="guide-search" v-model="search" placeholder="搜索标题、步骤或问题" clearable /><el-select v-model="client" aria-label="筛选软件" @change="load"><el-option value="all" label="全部软件" /><el-option v-for="item in catalog?.client_filters || []" :key="item.id" :value="item.id" :label="item.label" /></el-select><el-button type="primary" native-type="submit" :loading="busy">搜索</el-button></form>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
  <el-skeleton v-if="busy && !catalog" :rows="7" animated />
  <div v-if="catalog" class="guide-layout">
    <aside class="surface guide-list" aria-label="指南目录"><p class="small muted">{{ catalog.total }} 篇指南</p><button v-for="item in catalog.articles" :key="item.id" :class="{selected:selected === item.id}" :aria-pressed="selected === item.id" @click="selected = item.id"><strong>{{ item.title }}</strong><span>{{ item.category }} · {{ item.summary }}</span></button><p v-if="!catalog.total">没有匹配的指南，请换一个关键词。</p></aside>
    <article v-if="article" class="surface guide-content"><p class="eyebrow">{{ article.category }}</p><h2>{{ article.title }}</h2><p class="muted">{{ article.summary }}</p><el-alert v-if="article.verification.state !== 'not_applicable'" :title="article.verification.note" :type="article.verification.state === 'unsupported' ? 'info' : 'warning'" :closable="false" show-icon />
      <p v-for="(version,index) in article.versions.historical" :key="index" class="small muted">历史参考：{{ version.software || version.core }}（{{ version.date }}）。{{ version.note }}</p>
      <section v-for="section in article.sections" :key="section.title" class="account-section"><h3>{{ section.title }}</h3><p v-for="paragraph in section.paragraphs" :key="paragraph">{{ paragraph }}</p><ol v-if="section.steps.length" class="guide-steps"><li v-for="step in section.steps" :key="step">{{ step }}</li></ol><p v-for="note in section.notes" :key="note" class="inline-note">{{ note }}</p></section>
      <p class="small muted">内容修订：{{ catalog.content_revision }}。软件操作参考不等于你的设备已完成验证。</p>
    </article>
    <section v-else class="surface empty-state"><h2>没有匹配的内容</h2><p>试试“更新规则”“订阅”或“流量”。</p></section>
  </div>
</template>
