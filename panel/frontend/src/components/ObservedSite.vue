<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError, request } from '../api'
import { boundedRequest } from '../billingPending'
import { formatDate, formatGB } from '../display'

interface ObservedSiteData {
  schema_version: 1; source_label: string; captured_at: string | null; assembled_at: string
  units: { name: string; status: string }[]
  accounts: { total: number; active_admins: number }
  membership: { registered_quota_bytes: string; expires_at: string | null; provisioning_state: string; usage_state: string }
  rules: { id: string | number; action: 'direct' | 'proxy'; kind: string; value: string; scope_domain: string; enabled: boolean; revision: number }[]
  resources: { key: string; label: string; http_status: number | null; format: string; status: string; message: string }[]
  limitations: string[]
}
const emit = defineEmits<{ loaded: [available: boolean] }>()
const snapshot = ref<ObservedSiteData | null>(null), busy = ref(false), message = ref(''), search = ref('')
const rules = computed(() => {
  const query = search.value.trim().toLowerCase()
  return (snapshot.value?.rules || []).filter(rule => !query || [rule.value, rule.scope_domain, rule.kind, rule.action, String(rule.id)].some(value => value.toLowerCase().includes(query)))
})
const resourceFailures = computed(() => snapshot.value?.resources.filter(item => item.http_status !== 200) || [])
let generation = 0, disposed = false
function stateLabel(value: string) {
  return ({ active: '采集时运行', inactive: '采集时未运行', failed: '采集时失败', pending_apply: '等待应用', not_connected: '计量未接入', unknown: '未知' } as Record<string, string>)[value] || value
}
async function load() {
  if (disposed) return
  const current = ++generation
  busy.value = true; message.value = ''; snapshot.value = null
  try {
    const value = await boundedRequest(request<ObservedSiteData>('/admin/observed-site'), 15_000)
    if (current !== generation) return
    if (value.schema_version !== 1 || !value.assembled_at || !Array.isArray(value.rules) || !Array.isArray(value.resources) || !Array.isArray(value.units) || !Array.isArray(value.limitations) || !value.accounts || !value.membership) throw new Error('invalid_snapshot')
    snapshot.value = value; emit('loaded', true)
  } catch (error) {
    if (current !== generation) return
    message.value = error instanceof ApiError && error.status === 404 ? '线上只读资料尚未接入。' : '暂时无法读取线上只读资料，请稍后重试。'
    emit('loaded', false)
  } finally { if (current === generation) busy.value = false }
}
onMounted(load)
onBeforeUnmount(() => { disposed = true; generation++; snapshot.value = null })
</script>

<template>
  <section class="surface observed-site" aria-label="线上只读资料" :aria-busy="busy">
    <div class="section-title"><div><h2>线上只读资料</h2><p class="small muted">一次性采集快照，非实时监控；刷新仅重新读取已保存资料。</p></div><el-button :loading="busy" @click="load">刷新资料</el-button></div>
    <el-skeleton v-if="busy" :rows="3" animated />
    <p v-else-if="message" class="quality-note">{{ message }}</p>
    <template v-if="snapshot">
      <p class="small muted">来源：{{ snapshot.source_label }} · 快照整理时间：{{ formatDate(snapshot.assembled_at) }}</p>
      <p class="small muted">{{ snapshot.captured_at ? '记录采集时间：' + formatDate(snapshot.captured_at) : '各项资料没有统一采集时间；整理时间不代表现场采集时间。' }}</p>
      <dl class="observed-metrics"><div><dt>账号总数</dt><dd>{{ snapshot.accounts.total }}</dd></div><div><dt>启用管理员</dt><dd>{{ snapshot.accounts.active_admins }}</dd></div><div><dt>规则数量</dt><dd>{{ snapshot.rules.length }}</dd></div><div><dt>资源数量</dt><dd>{{ snapshot.resources.length }}</dd></div></dl>
      <h3>采集时的服务状态</h3>
      <dl class="observed-units"><div v-for="unit in snapshot.units" :key="unit.name"><dt>{{ unit.name }}</dt><dd>{{ stateLabel(unit.status) }}</dd></div></dl>
      <p class="quality-note">会员额度 {{ formatGB(snapshot.membership.registered_quota_bytes) }}：仅登记额度，尚未应用，不代表可用余额或可靠计量。</p>
      <p class="small muted">开通状态：{{ stateLabel(snapshot.membership.provisioning_state) }} · 用量状态：{{ stateLabel(snapshot.membership.usage_state) }} · 登记到期时间：{{ snapshot.membership.expires_at ? snapshot.membership.expires_at + '（时区待核）' : '暂不可确认' }}</p>
      <div class="section-title"><h3>只读规则</h3><el-input v-model="search" placeholder="搜索规则或适用域名" aria-label="搜索只读规则" clearable class="rule-search" /></div>
      <p class="small muted">共 {{ snapshot.rules.length }} 条，当前显示 {{ rules.length }} 条；这里不修改规则或发布配置。</p>
      <div class="observed-table-scroll"><table><thead><tr><th>规则</th><th>动作</th><th>类型</th><th>适用域名</th><th>状态</th><th>修订</th></tr></thead><tbody><tr v-for="rule in rules" :key="rule.id"><td>{{ rule.value }}</td><td>{{ rule.action === 'direct' ? '直连' : '代理' }}</td><td>{{ rule.kind }}</td><td>{{ rule.scope_domain || '未限定' }}</td><td>{{ rule.enabled ? '启用' : '停用' }}</td><td>{{ rule.revision }}</td></tr></tbody></table></div>
      <p v-if="!rules.length" class="muted">没有匹配的规则。</p>
      <h3>资源响应核验</h3>
      <p class="small muted">HTTP 200 仅表示当时响应成功，不代表客户端已导入、已应用或连接可用。</p>
      <el-alert v-for="resource in resourceFailures" :key="resource.key" :title="resource.label + '：' + (resource.http_status === null ? '未确认响应' : 'HTTP ' + resource.http_status)" :description="resource.message" type="warning" :closable="false" show-icon class="spaced" />
      <div class="observed-table-scroll"><table><thead><tr><th>资源</th><th>HTTP</th><th>格式</th><th>核验说明</th></tr></thead><tbody><tr v-for="resource in snapshot.resources" :key="resource.key"><td>{{ resource.label }}</td><td><el-tag :type="resource.http_status === 200 ? 'info' : 'warning'">{{ resource.http_status ?? '未确认' }}</el-tag></td><td>{{ resource.format || '未确认' }}</td><td>{{ resource.message || resource.status }}</td></tr></tbody></table></div>
      <ul v-if="snapshot.limitations.length" class="small muted"><li v-for="(item, index) in snapshot.limitations" :key="index">{{ item }}</li></ul>
    </template>
  </section>
</template>

<style scoped>
.observed-site { padding: 24px; margin-bottom: 24px; }
.observed-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.observed-metrics dt, .observed-units dt { color: var(--muted); font-size: 13px; }
.observed-metrics dd { margin: 8px 0 0; font-size: 26px; }
.observed-units { display: flex; flex-wrap: wrap; gap: 20px; }
.observed-units dd { margin: 6px 0; }
.rule-search { width: min(100%, 320px); }
.observed-table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; border-bottom: 1px solid var(--border); padding: 12px 10px; overflow-wrap: anywhere; }
th { white-space: nowrap; }
@media(max-width: 640px) { .observed-site { padding: 18px; } .observed-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
