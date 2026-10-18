<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, errorMessage, request } from '../api'
import { formatDate } from '../display'
import type { InventoryList, ServerInventory, ServerInventoryDetail, ResourceMetadata } from '../inventoryTypes'
import type { Pagination } from '../types'
import InventoryMetadataEditor from '../components/InventoryMetadataEditor.vue'
import InventoryChecks from '../components/InventoryChecks.vue'
import RefreshControl from '../components/RefreshControl.vue'
import ServerInventoryCard from '../components/ServerInventoryCard.vue'
import ServerCoreDetails from '../components/ServerCoreDetails.vue'
import ServerObservationView from '../components/ServerObservationView.vue'
import ResourceUsageCard from '../components/ResourceUsageCard.vue'
import ResourcePlanEditor from '../components/ResourcePlanEditor.vue'
import NodeResourceList from '../components/NodeResourceList.vue'
import { useInventoryClock } from '../inventoryStatus'
const observationClock = useInventoryClock()
const route = useRoute(), router = useRouter()
const items = ref<ServerInventory[]>([]), busy = ref(true), error = ref('')
const q = ref(''), status = ref('all'), page = ref(1), view = ref<'cards' | 'compact'>('cards')
const lastReadAt = ref<string | null>(null), detailReadAt = ref<string | null>(null)
const pagination = ref<Pagination>({ page: 1, page_size: 25, total: 0, pages: 0, has_next: false, has_previous: false })
const selected = ref<Pick<ServerInventory, 'id' | 'name'> | null>(null), detail = ref<ServerInventoryDetail | null>(null)
const detailBusy = ref(false), detailError = ref('')
const statuses = [{ id: 'all', label: '全部登记状态' }, { id: 'enabled', label: '登记启用' }, { id: 'disabled', label: '登记停用' }]
const sections = [{ id: 'overview', label: '概览' }, { id: 'cores', label: '核心' }, { id: 'nodes', label: '节点' }, { id: 'usage', label: '用量与有效期' }, { id: 'checks', label: '检查记录' }] as const
type Section = typeof sections[number]['id']
const activeSection = ref<Section>('overview')
const sampledAt = computed(() => items.value.map(item => item.observation?.observed_at).filter((value): value is string => Boolean(value)).sort().at(-1) || null)
let generation = 0, detailGeneration = 0, filterKey = ''
let pendingList: { key: string; promise: Promise<void> } | null = null
let pendingDetail: { key: string; promise: Promise<void> } | null = null
let listController: AbortController | null = null, detailController: AbortController | null = null
function invalidated(e: unknown) { return e instanceof ApiError && [401, 403, 404].includes(e.status) }
function retainPurchaseMeter(next: ServerInventoryDetail): ServerInventoryDetail {
  const previous = detail.value
  // 仅在相同已授权绑定的来源暂不可读时保旧；解绑或撤权立即撤除采购数据。
  if (next.purchase_meter_state !== 'unavailable' || !next.purchase_binding_key || next.purchase_binding_key !== previous?.purchase_binding_key || next.server.id !== previous.server.id || !previous.purchase_meter) return next
  return { ...next, purchase_meter: { ...previous.purchase_meter, quality: 'error', alerts: [...previous.purchase_meter.alerts.filter(alert => alert.code !== 'purchase_source_unavailable'), { code: 'purchase_source_unavailable', severity: 'warning', message: '采购来源暂不可读取，已保留上次记录与原采样时间。' }] } }
}
function closeDetail() { detailGeneration++; detailController?.abort(); pendingDetail = null; selected.value = null; detail.value = null; detailBusy.value = false; detailError.value = ''; detailReadAt.value = null }
function load(): Promise<void> {
  const path = '/admin/servers?' + new URLSearchParams({ q: q.value.trim(), status: status.value, page: String(page.value), page_size: '25' })
  if (pendingList?.key === path) return pendingList.promise
  const current = ++generation
  listController?.abort()
  const controller = new AbortController(); listController = controller
  const timer = setTimeout(() => controller.abort(), 15_000)
  busy.value = true; error.value = ''
  const promise = (async () => {
    try {
      const data = await request<InventoryList<ServerInventory>>(path, undefined, undefined, controller.signal)
      if (current !== generation) return
      items.value = data.items; pagination.value = data.pagination; lastReadAt.value = new Date().toISOString()
      const existing = selected.value && data.items.find(item => item.id === selected.value?.id)
      if (existing) selected.value = existing
      if (selected.value) await showDetail(selected.value)
    } catch (e) {
      if (current !== generation) return
      error.value = controller.signal.aborted ? '读取超时，请重试。' : errorMessage(e)
      if (invalidated(e)) { items.value = []; lastReadAt.value = null; closeDetail() }
    } finally { clearTimeout(timer); if (current === generation) { busy.value = false; pendingList = null; listController = null } }
  })()
  pendingList = { key: path, promise }
  return promise
}
function showDetail(server: Pick<ServerInventory, 'id' | 'name'>): Promise<void> {
  if (pendingDetail?.key === server.id) return pendingDetail.promise
  const current = ++detailGeneration
  detailController?.abort()
  const controller = new AbortController(); detailController = controller
  const timer = setTimeout(() => controller.abort(), 15_000)
  if (selected.value?.id !== server.id) { detail.value = null; detailReadAt.value = null }
  selected.value = server; detailError.value = ''; detailBusy.value = true
  const promise = (async () => {
    try {
      const data = await request<ServerInventoryDetail>('/admin/servers/' + encodeURIComponent(server.id), undefined, undefined, controller.signal)
      if (current === detailGeneration) { detail.value = retainPurchaseMeter(data); selected.value = data.server; detailReadAt.value = new Date().toISOString() }
    } catch (e) {
      if (current !== detailGeneration) return
      detailError.value = controller.signal.aborted ? '详情读取超时，请重试。' : errorMessage(e)
      if (invalidated(e)) { detail.value = null; detailReadAt.value = null }
    } finally { clearTimeout(timer); if (current === detailGeneration) { detailBusy.value = false; pendingDetail = null; detailController = null } }
  })()
  pendingDetail = { key: server.id, promise }
  return promise
}
function refreshDetail() { if (selected.value) void showDetail(selected.value) }
function openDetail(server: ServerInventory, section: Section = 'overview') {
  activeSection.value = section
  void router.replace({ path: '/admin/servers', query: { ...route.query, server_id: server.id, section } })
  void showDetail(server)
}
function closeDrawer() {
  closeDetail()
  if (route.query.server_id) { const { server_id: _id, section: _section, ...query } = route.query; void router.replace({ path: '/admin/servers', query }) }
}
function selectSection(section: Section) {
  activeSection.value = section
  if (selected.value) void router.replace({ path: '/admin/servers', query: { ...route.query, server_id: selected.value.id, section } })
}
async function updateFilters(nextPage: number) {
  const query = { ...(q.value.trim() ? { q: q.value.trim() } : {}), ...(status.value !== 'all' ? { status: status.value } : {}), ...(nextPage > 1 ? { page: String(nextPage) } : {}) }
  const same = q.value.trim() === (route.query.q || '') && status.value === (route.query.status || 'all') && nextPage === Number(route.query.page || 1)
  page.value = nextPage
  if (same) await load()
  else await router.replace({ path: '/admin/servers', query })
}
function search() { void updateFilters(1) }
function changePage(value: number) { void updateFilters(value) }
function recordedAt(value: string | null) { return value ? formatDate(value) : '暂无记录' }
function metadataSaved(item: ResourceMetadata) {
  items.value = items.value.map(row => row.id === item.id ? { ...row, ...item } : row)
  if (selected.value?.id === item.id) selected.value = { ...selected.value, ...item }
  if (detail.value?.server.id === item.id) detail.value.server = { ...detail.value.server, ...item }
}
function resourceMetadataSaved(item: ResourceMetadata) {
  if (!detail.value) return
  detail.value.cores.items = detail.value.cores.items.map(core => core.id === item.id ? { ...core, ...item } : core)
  if (detail.value.nodes) detail.value.nodes.items = detail.value.nodes.items.map(node => node.id === item.id ? { ...node, ...item } : node)
}
watch(() => route.query, query => {
  q.value = typeof query.q === 'string' ? query.q : ''
  status.value = typeof query.status === 'string' && statuses.some(item => item.id === query.status) ? query.status : 'all'
  const requested = typeof query.page === 'string' ? Number(query.page) : 1
  page.value = Number.isSafeInteger(requested) && requested > 0 ? requested : 1
  const nextFilter = JSON.stringify([q.value, status.value, page.value])
  if (filterKey !== nextFilter) { closeDetail(); filterKey = nextFilter; void load() }
  const section = sections.find(item => item.id === query.section)
  activeSection.value = section?.id || 'overview'
  if (typeof query.server_id === 'string' && query.server_id && selected.value?.id !== query.server_id) {
    void showDetail(items.value.find(item => item.id === query.server_id) || { id: query.server_id, name: '服务器详情' })
  } else if (!query.server_id && selected.value) closeDetail()
}, { immediate: true })
function refreshObservedData() {
  if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
  if (lastReadAt.value && !busy.value && !detailBusy.value) void load()
}
// 页面仅限频读取现有投影，不触发主机采样或供应商查询。
const refreshTimer = setInterval(refreshObservedData, 60_000)
onUnmounted(() => { clearInterval(refreshTimer); generation++; listController?.abort(); pendingList = null; closeDetail() })
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员工作区</p><h1>服务器</h1><p class="muted">机器状态、核心实例与真实节点。</p></div><RefreshControl :loading="busy" :error="error" :last-read-at="lastReadAt" :sampled-at="sampledAt" @refresh="load" /></div>
  <p class="small muted inventory-note">登记启用不代表在线。页面可见时每60秒读取已采集数据；最后记录与来源采样各自保留原始时间。</p>
  <section class="surface admin-panel">
    <form class="table-filters" @submit.prevent="search"><label class="visually-hidden" for="server-search">搜索服务器或适配器</label><el-input id="server-search" v-model="q" placeholder="搜索服务器或适配器" clearable /><el-select v-model="status" aria-label="服务器登记状态" @change="search"><el-option v-for="item in statuses" :key="item.id" :value="item.id" :label="item.label" /></el-select><el-button type="primary" native-type="submit" :loading="busy">搜索</el-button></form>
    <div class="collection-toolbar"><span class="small muted">{{ pagination.total }} 台服务器</span><div class="view-switch" role="group" aria-label="服务器显示方式"><button :aria-pressed="view === 'cards'" @click="view = 'cards'">卡片</button><button :aria-pressed="view === 'compact'" @click="view = 'compact'">紧凑列表</button></div></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
    <el-skeleton v-if="busy && !lastReadAt" :rows="8" animated />
    <div v-else-if="items.length" class="server-grid" :class="{ 'is-compact': view === 'compact' }" :aria-busy="busy"><ServerInventoryCard v-for="server in items" :key="server.id" :server="server" :now="observationClock" :compact="view === 'compact'" @open="section => openDetail(server, section)" /></div>
    <div v-else-if="!error" class="empty-state"><h2>没有符合条件的服务器</h2><p class="muted">可调整搜索和登记状态；尚未接入的真实资产需先完成资源盘点与导入预览。</p></div>
    <div class="table-bottom"><p class="small muted">来源与采样时间随每台机器分别展示。</p><el-pagination :current-page="pagination.page" :page-size="pagination.page_size" :total="pagination.total" layout="total, prev, pager, next" @current-change="changePage" /></div>
  </section>
  <el-drawer :model-value="selected !== null" :title="selected?.name || '服务器详情'" size="min(780px, 100vw)" @close="closeDrawer">
    <RefreshControl :loading="detailBusy" :error="detailError" :source-error="detail?.purchase_meter_state === 'unavailable' ? '采购来源暂不可读取' : ''" :last-read-at="detailReadAt" :sampled-at="detail?.observation?.observed_at" @refresh="refreshDetail" />
    <nav class="detail-tabs" aria-label="服务器详情分区"><button v-for="section in sections" :key="section.id" :aria-current="activeSection === section.id ? 'page' : undefined" @click="selectSection(section.id)">{{ section.label }}</button></nav>
    <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" show-icon />
    <el-skeleton v-if="detailBusy && !detail" :rows="7" animated />
    <template v-if="detail">
      <section v-show="activeSection === 'overview'" aria-label="服务器概览">
        <ServerObservationView :observation="detail.observation" />
        <dl class="inventory-facts"><div><dt>服务器编号</dt><dd>{{ detail.server.id }}</dd></div><div><dt>登记状态</dt><dd>{{ detail.server.enabled ? '登记启用' : '登记停用' }}</dd></div><div><dt>最后记录</dt><dd>{{ recordedAt(detail.server.last_seen_at) }}</dd></div><div><dt>适配器</dt><dd>{{ detail.server.adapter === 'unconfigured' ? '未配置' : detail.server.adapter }}</dd></div></dl>
        <InventoryMetadataEditor :key="detail.server.id" :item="detail.server" kind="servers" :enabled="detail.capabilities.edit" @saved="metadataSaved" />
      </section>
      <div v-show="activeSection === 'cores'"><ServerCoreDetails :key="detail.server.id" :cores="detail.cores" :metadata-editable="detail.capabilities.edit" @saved="resourceMetadataSaved" /></div>
      <section v-show="activeSection === 'nodes'" aria-label="服务器节点">
        <div class="section-title"><h2>节点与端点</h2><RouterLink :to="{ path: '/admin/lines', query: { server_id: detail.server.id } }">查看关联线路 →</RouterLink></div>
        <NodeResourceList v-if="detail.nodes" :nodes="detail.nodes.items" :metadata-editable="detail.capabilities.edit" @saved="resourceMetadataSaved" />
        <p v-if="detail.nodes?.truncated" class="small muted">仅展示前 {{ detail.nodes.items.length }} 个节点，总数 {{ detail.nodes.total }}。</p>
        <details class="endpoint-register"><summary>原入口与出口登记</summary>
        <h3>入口（{{ detail.ingresses.total }}）</h3><ul v-if="detail.ingresses.items.length" class="endpoint-list"><li v-for="entry in detail.ingresses.items" :key="entry.id"><span>{{ entry.name }}</span><span class="small muted">{{ entry.protocol_label }} · {{ entry.enabled ? '登记启用' : '登记停用' }}</span></li></ul><p v-else class="muted">尚无入口登记。</p>
        <p v-if="detail.ingresses.truncated" class="small muted">仅展示前 {{ detail.ingresses.items.length }} 个入口，总数 {{ detail.ingresses.total }}。</p>
        <h3>出口（{{ detail.egresses.total }}）</h3><ul v-if="detail.egresses.items.length" class="endpoint-list"><li v-for="entry in detail.egresses.items" :key="entry.id"><span>{{ entry.name }}</span><span class="small muted">{{ entry.kind_label }}</span></li></ul><p v-else class="muted">尚无出口登记。</p>
        <p v-if="detail.egresses.truncated" class="small muted">仅展示前 {{ detail.egresses.items.length }} 个出口，总数 {{ detail.egresses.total }}。</p>
        </details>
      </section>
      <section v-show="activeSection === 'usage'" aria-label="机器用量与有效期"><h2>用量与有效期</h2><p class="quality-note">此处分开展示机器采购用量和有效期；不与个人套餐相加，也不从网卡累计推导套餐账单。</p><template v-if="detail.purchase_meter"><ResourceUsageCard :meter="detail.purchase_meter" :now="observationClock" /><ResourcePlanEditor v-if="detail.purchase_binding_key" :binding-key="detail.purchase_binding_key" :enabled="detail.purchase_plan_editable === true" @saved="refreshDetail" /></template><p v-else class="muted">机器采购用量尚未关联可用来源；额度、重置与到期暂不可确认。需将已核对的机器采购来源绑定至此服务器。</p></section>
      <section v-show="activeSection === 'checks'" aria-label="服务器检查记录"><InventoryChecks :checks="detail.checks" :actions="detail.check_actions || []" @completed="refreshDetail" /></section>
    </template>
  </el-drawer>
</template>
<style scoped>
.inventory-note{margin-bottom:20px}.collection-toolbar{display:flex;justify-content:space-between;align-items:center;gap:16px;margin:18px 0}.view-switch{display:flex;gap:3px;background:#f0f3f8;border-radius:9px;padding:3px}.view-switch button,.detail-tabs button{border:0;font:inherit;font-size:13px;cursor:pointer;padding:9px 12px;background:transparent;color:#63748f;border-radius:7px;min-height:38px}.view-switch button[aria-pressed=true]{background:white;color:#365ccb;box-shadow:0 2px 5px #1d3f7110}.server-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,310px),1fr));gap:18px}.server-grid.is-compact{grid-template-columns:1fr}.detail-tabs{display:flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid #e6ebf3;margin:22px 0;padding-bottom:8px}.detail-tabs button[aria-current=page]{background:#eef3ff;color:#365ccb;font-weight:600}.inventory-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin:24px 0}.inventory-facts dt{font-size:13px;color:#75849b}.inventory-facts dd{margin:6px 0 0;font-weight:600;overflow-wrap:anywhere}.endpoint-list{list-style:none;padding:0;margin:12px 0 24px}.endpoint-list li{display:flex;justify-content:space-between;gap:14px;padding:11px 0;border-bottom:1px solid #e7ecf4}.empty-state{text-align:center;padding:28px 16px}.empty-state h2{font-size:18px}@media(max-width:520px){.inventory-facts{grid-template-columns:1fr}.endpoint-list li{flex-direction:column;gap:4px}.table-filters{flex-wrap:wrap}.table-filters>*{min-width:0;max-width:100%}.collection-toolbar{gap:8px}.view-switch button{padding:8px 10px}.detail-tabs{gap:2px}.detail-tabs button{padding:9px}.table-bottom{flex-wrap:wrap;gap:8px}.table-bottom :deep(.el-pagination){max-width:100%;flex-wrap:wrap}.section-title{flex-wrap:wrap;gap:8px}}
</style>
