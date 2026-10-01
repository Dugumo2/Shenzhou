<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request, errorMessage } from '../api'
import { formatDate } from '../display'
interface Rule { id: number; action: string; kind: string; value: string | null; scope_domain: string | null; validation: string; enabled: boolean; revision: number; source_id: string }
interface Source { id: string; name: string; kind: string; state: string; rule_count: number | null; sha256: string | null; updated_at: string | null; version: number | null; generator: string; order_semantics: string }
interface Rules { read_only: boolean; scope: string; sources: Source[]; items: Rule[]; publish_state: string; client_apply_state: string; message: string }
const data = ref<Rules | null>(null), busy = ref(true), error = ref(''), search = ref('')
const filtered = computed(() => data.value?.items.filter(rule => (rule.value || rule.scope_domain || '').toLowerCase().includes(search.value.toLowerCase())) || [])
const sourceStates: Record<string, string> = { candidate: '候选数据', invalid: '需要核对', validation_only: '仅校验依据', not_connected: '尚未接入' }
const actionLabels: Record<string, string> = { client_direct: '客户端本地直连', server_direct: '服务器直出', proxy: '代理', block: '阻断' }
const kindLabels: Record<string, string> = { domain: '完整域名', suffix: '域名后缀', regex: '正则表达式', domain_suffix: '域名后缀', domain_regex: '正则表达式' }
async function load() {
  busy.value = true; error.value = ''
  try { data.value = await request<Rules>('/admin/rules') }
  catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
onMounted(load)
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员 · 只读</p><h1>代理规则</h1><p class="muted">核对规则与来源，发布和客户端应用状态分别查看。</p></div><el-button :loading="busy" @click="load">刷新</el-button></div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
  <el-skeleton v-if="busy && !data" :rows="6" animated />
  <template v-if="data"><el-alert :title="data.message" type="info" show-icon :closable="false" class="spaced" />
    <div class="source-grid"><article v-for="source in data.sources" :key="source.id" class="surface source-card"><div class="card-heading"><h2>{{ source.name }}</h2><el-tag type="info">{{ sourceStates[source.state] || '待核对' }}</el-tag></div><dl class="card-facts"><div><dt>规则数量</dt><dd>{{ source.rule_count === null ? '待核对' : source.rule_count }}</dd></div><div><dt>版本</dt><dd>{{ source.version === null ? '待核对' : source.version }}</dd></div></dl><p class="small muted">更新时间：{{ formatDate(source.updated_at) }}<br>生成方式：{{ source.generator }}<br>顺序：{{ source.order_semantics }}</p><p v-if="source.sha256" class="small muted source-hash">内容摘要：{{ source.sha256 }}</p></article></div>
    <section class="surface admin-panel"><div class="section-title"><h2>自建规则</h2><label class="visually-hidden" for="rule-search">搜索规则内容</label><el-input id="rule-search" v-model="search" placeholder="搜索域名或规则内容" clearable class="rule-search" /></div>
      <div v-if="!data.items.length" class="inline-note">当前候选数据库尚未导入自建规则。这里没有读取生产规则，不能据此判断原规则丢失；需按来源清单核对后导入。</div>
      <div v-else class="table-scroll"><el-table :data="filtered" row-key="id" empty-text="没有匹配的规则"><el-table-column label="规则内容" min-width="250"><template #default="{ row }">{{ row.value || row.scope_domain || '待核对' }}</template></el-table-column><el-table-column label="类型" min-width="125"><template #default="{ row }">{{ kindLabels[row.kind] || row.kind }}</template></el-table-column><el-table-column label="动作" min-width="150"><template #default="{ row }">{{ actionLabels[row.action] || '待核对' }}</template></el-table-column><el-table-column label="状态" min-width="130"><template #default="{ row }">{{ row.validation !== 'valid' ? '需要核对' : row.enabled ? '已启用（候选）' : '已禁用' }}</template></el-table-column><el-table-column label="版本" min-width="90" prop="revision" /></el-table></div>
      <div class="release-facts"><span>服务器发布：{{ data.publish_state === 'unknown' ? '待确认' : data.publish_state }}</span><span>客户端应用：{{ data.client_apply_state === 'unknown' ? '待确认' : data.client_apply_state }}</span></div>
    </section>
  </template>
</template>
