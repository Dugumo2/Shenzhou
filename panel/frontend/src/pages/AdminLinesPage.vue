<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, errorMessage, request } from '../api'
import type { InventoryList, LineInventory, LineInventoryDetail } from '../inventoryTypes'
import type { Pagination } from '../types'
import type { ResourceMetadata } from '../inventoryTypes'
import InventoryMetadataEditor from '../components/InventoryMetadataEditor.vue'
import InventoryChecks from '../components/InventoryChecks.vue'
import RefreshControl from '../components/RefreshControl.vue'
import LineInventoryCard from '../components/LineInventoryCard.vue'
import LinePathDiagram from '../components/LinePathDiagram.vue'
import NodeResourceList from '../components/NodeResourceList.vue'
import { effectiveFreshness, useInventoryClock } from '../inventoryStatus'
const observationClock = useInventoryClock()

const route = useRoute(), router = useRouter()
const items = ref<LineInventory[]>([]), busy = ref(true), error = ref('')
const viewMode = ref<'cards' | 'list'>('cards'), lastReadAt = ref<string | null>(null)
const q = ref(''), status = ref('all'), serverId = ref(''), page = ref(1)
const pagination = ref<Pagination>({ page: 1, page_size: 25, total: 0, pages: 0, has_next: false, has_previous: false })
const selected = ref<LineInventory | null>(null), detail = ref<LineInventoryDetail | null>(null)
const detailBusy = ref(false), detailError = ref('')
const detailReadAt = ref<string | null>(null)
const statuses = [{ id: 'all', label: '全部登记状态' }, { id: 'enabled', label: '登记启用' }, { id: 'disabled', label: '登记停用' }]
let generation = 0, detailGeneration = 0
let pendingList: Promise<void> | null = null, pendingListKey = ''
let listController: AbortController | null = null, detailController: AbortController | null = null
function closeDetail() { detailGeneration++; detailController?.abort(); detailController = null; selected.value = null; detail.value = null; detailBusy.value = false; detailError.value = ''; detailReadAt.value = null }
function permissionLost(e: unknown) { return e instanceof ApiError && (e.status === 401 || e.status === 403) }
function clearPrivateContent(e: unknown) {
  // 权限失效与暂时读取失败分开；取消请求并拒绝迟到响应重新填入私有内容。
  generation++; listController?.abort(); listController = null; pendingList = null; pendingListKey = ''
  closeDetail(); items.value = []; lastReadAt.value = null; busy.value = false
  pagination.value = { page: 1, page_size: 25, total: 0, pages: 0, has_next: false, has_previous: false }
  error.value = errorMessage(e)
}
function load(): Promise<void> {
  const key = JSON.stringify([q.value.trim(), status.value, serverId.value, page.value])
  if (pendingList && pendingListKey === key) return pendingList
  pendingListKey = key
  const running = fetchList()
  pendingList = running
  void running.finally(() => { if (pendingList === running) pendingList = null })
  return running
}
async function fetchList() {
  const current = ++generation
  listController?.abort()
  const controller = new AbortController(); listController = controller
  busy.value = true; error.value = ''
  try {
    const params = new URLSearchParams({ q: q.value.trim(), status: status.value, page: String(page.value), page_size: '25' })
    if (serverId.value) params.set('server_id', serverId.value)
    const data = await request<InventoryList<LineInventory>>('/admin/lines?' + params, undefined, undefined, controller.signal)
    if (current === generation) {
      items.value = data.items; pagination.value = data.pagination; lastReadAt.value = new Date().toISOString()
      if (selected.value) {
        const refreshed = data.items.find(row => row.id === selected.value?.id)
        if (refreshed) selected.value = refreshed
        void showDetail(selected.value)
      }
    }
  } catch (e) { if (current === generation) { if (permissionLost(e)) clearPrivateContent(e); else error.value = errorMessage(e) } }
  finally { if (current === generation) busy.value = false }
}
async function showDetail(line: LineInventory) {
  if (detailBusy.value && selected.value?.id === line.id) return
  const current = ++detailGeneration
  detailController?.abort()
  const controller = new AbortController(); detailController = controller
  if (selected.value?.id !== line.id) { detail.value = null; detailReadAt.value = null }
  selected.value = line; detailError.value = ''; detailBusy.value = true
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    const deadline = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        controller.abort()
        reject(new ApiError(0, 'DETAIL_TIMEOUT', detail.value ? '详情读取超时，已保留上次内容，请重试。' : '详情读取超时，请重试。'))
      }, 15_000)
    })
    const data = await Promise.race([request<LineInventoryDetail>('/admin/lines/' + encodeURIComponent(line.id), undefined, undefined, controller.signal), deadline])
    if (current === detailGeneration && !controller.signal.aborted) { detail.value = data; detailReadAt.value = new Date().toISOString() }
  } catch (e) { if (current === detailGeneration) { if (permissionLost(e)) clearPrivateContent(e); else detailError.value = errorMessage(e) } }
  finally { clearTimeout(timer); if (current === detailGeneration) detailBusy.value = false }
}
async function updateFilters(nextPage: number) {
  const query = { ...(q.value.trim() ? { q: q.value.trim() } : {}), ...(status.value !== 'all' ? { status: status.value } : {}), ...(serverId.value ? { server_id: serverId.value } : {}), ...(nextPage > 1 ? { page: String(nextPage) } : {}) }
  const same = q.value.trim() === (route.query.q || '') && status.value === (route.query.status || 'all') && serverId.value === (route.query.server_id || '') && nextPage === Number(route.query.page || 1)
  page.value = nextPage
  if (same) await load()
  else await router.replace({ path: '/admin/lines', query })
}
function search() { void updateFilters(1) }
function changePage(value: number) { void updateFilters(value) }
function clearServer() { serverId.value = ''; search() }
function verificationLabel(line: LineInventory) {
  const value = line.verification
  const state = effectiveFreshness(value.state, value.expires_at, observationClock.value)
  if (state === 'fresh') return ({ pass:'整链通过（有效期内）', fail:'整链失败', timeout:'整链超时', unknown:'结果未知' } as Record<string,string>)[value.result || 'unknown']
  return ({ stale:'检测已过期', target_changed:'路径已变化', unknown:'未知' } as Record<string,string>)[state] || '尚未检测'
}
function resourceMetadataSaved(item: ResourceMetadata) {
  if (detail.value?.nodes) detail.value.nodes.items = detail.value.nodes.items.map(node => node.id === item.id ? { ...node, ...item } : node)
}
function metadataSaved(item: ResourceMetadata) {
  items.value = items.value.map(row => row.id === item.id ? { ...row, ...item } : row)
  if (selected.value?.id === item.id) selected.value = { ...selected.value, ...item }
  if (detail.value?.line.id === item.id) detail.value.line = { ...detail.value.line, ...item }
}
watch(() => route.query, query => {
  closeDetail()
  q.value = typeof query.q === 'string' ? query.q : ''
  status.value = typeof query.status === 'string' && statuses.some(item => item.id === query.status) ? query.status : 'all'
  serverId.value = typeof query.server_id === 'string' ? query.server_id : ''
  const requested = typeof query.page === 'string' ? Number(query.page) : 1
  page.value = Number.isSafeInteger(requested) && requested > 0 ? requested : 1
  void load()
}, { immediate: true })
onUnmounted(() => { generation++; listController?.abort(); closeDetail() })
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员工作区</p><h1>线路</h1><p class="muted">查看已有线路的入口、出口、路径证据与独立检查。</p></div><RefreshControl :loading="busy" :error="error" :last-read-at="lastReadAt" @refresh="load" /></div>
  <el-alert title="完整上下游编排与路径编辑尚未开放；图中的登记关联不等于整链已验证。单节点与外部来源在服务器详情的节点分区查看。" type="info" show-icon :closable="false" class="inventory-note" />
  <section class="surface admin-panel">
    <form class="table-filters" @submit.prevent="search"><label class="visually-hidden" for="line-search">搜索线路、端点、协议或服务器</label><el-input id="line-search" v-model="q" placeholder="搜索线路、端点、协议或服务器" clearable /><el-select v-model="status" aria-label="线路登记状态" @change="search"><el-option v-for="item in statuses" :key="item.id" :value="item.id" :label="item.label" /></el-select><el-button type="primary" native-type="submit" :loading="busy">搜索</el-button></form>
    <div v-if="serverId" class="association-filter"><span class="small muted">按服务器 #{{ serverId.slice(0, 8) }} 查看关联线路（含主入口、附加入口和出口）。</span><el-button link type="primary" @click="clearServer">清除关联筛选</el-button></div>
    <div class="line-view-controls"><span class="small muted">{{ pagination.total }} 条已有线路</span><div role="group" aria-label="线路显示方式"><el-button :aria-pressed="viewMode === 'cards'" :type="viewMode === 'cards' ? 'primary' : 'default'" @click="viewMode = 'cards'">卡片</el-button><el-button :aria-pressed="viewMode === 'list'" :type="viewMode === 'list' ? 'primary' : 'default'" @click="viewMode = 'list'">紧凑列表</el-button></div></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
    <el-skeleton v-if="busy && !lastReadAt" :rows="6" animated />
    <template v-else><div v-if="viewMode === 'cards'" class="line-card-grid" :aria-busy="busy"><LineInventoryCard v-for="line in items" :key="line.id" :line="line" :verification="verificationLabel(line)" :selected="selected?.id === line.id" @open="showDetail(line)" /><p v-if="!items.length" class="muted">没有符合条件的线路登记。</p></div><div v-else class="table-scroll"><el-table :data="items" row-key="id" empty-text="没有符合条件的线路登记">
      <el-table-column label="线路" min-width="180"><template #default="{ row }"><el-button link type="primary" @click="showDetail(row)">{{ row.name }}</el-button><div class="small muted">#{{ row.id.slice(0, 8) }}</div></template></el-table-column>
      <el-table-column label="入口服务器 / 协议" min-width="230"><template #default="{ row }"><div v-for="entry in row.ingresses.slice(0, 3)" :key="entry.id" class="line-entry">{{ entry.server.name }} <span class="small muted">/ {{ entry.protocol_label }}</span></div><div v-if="row.ingress_count > 3" class="small muted">共 {{ row.ingress_count }} 个入口，详情查看。</div></template></el-table-column>
      <el-table-column label="出口服务器 / 类型" min-width="190"><template #default="{ row }">执行：{{ (row.path?.terminal_egress || row.egress).server.name }}<div class="small muted">{{ (row.path?.terminal_egress || row.egress).ownership === 'external' ? '外部资源 · ' : '' }}{{ (row.path?.terminal_egress || row.egress).kind_label }} · {{ (row.path?.terminal_egress || row.egress).name }}</div><div v-if="row.path?.terminal_egress && row.path.terminal_egress.id !== row.egress.id" class="small muted">经 {{ row.egress.name }}</div></template></el-table-column>
      <el-table-column label="线路登记" min-width="115"><template #default="{ row }"><el-tag type="info">{{ row.enabled ? '登记启用' : '登记停用' }}</el-tag></template></el-table-column>
      <el-table-column label="端点登记" min-width="145"><template #default="{ row }"><span :class="{ muted: row.endpoint_registration === 'enabled' }">{{ row.endpoint_registration === 'enabled' ? '登记均启用' : '含停用登记' }}</span></template></el-table-column>
      <el-table-column label="整链检测" min-width="170"><template #default="{ row }"><span class="muted">{{ verificationLabel(row) }}</span></template></el-table-column>
      <el-table-column label="来源 / 最近检查" min-width="210"><template #default="{ row }"><span>{{ row.path?.source === 'inventory_registration' ? '现有资源登记' : row.path?.source || '暂无路径证据' }}</span><div class="small muted">{{ row.verification.observed_at || '尚无整链回执' }}</div></template></el-table-column>
      <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" @click="showDetail(row)">查看详情</el-button></template></el-table-column>
    </el-table></div><div class="table-bottom"><p class="small muted">登记状态与实际可用性分开，路径编辑与发布尚未接通。</p><el-pagination :current-page="pagination.page" :page-size="pagination.page_size" :total="pagination.total" layout="total, prev, pager, next" @current-change="changePage" /></div></template>
  </section>
  <el-drawer :model-value="selected !== null" :title="selected?.name || '线路详情'" size="min(740px, 100vw)" @close="closeDetail">
    <RefreshControl :loading="detailBusy" :error="detailError" :last-read-at="detailReadAt" @refresh="selected && showDetail(selected)" />
    <el-skeleton v-if="detailBusy && !detail" :rows="7" animated />
    <p v-else-if="detailBusy" role="status" class="small muted">正在读取详情，保留上次内容…</p>
    <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" show-icon />
    <template v-if="detail">
      <p class="small muted">编号 {{ detail.line.id }}</p>
      <p class="small muted target-version">目标版本 {{ detail.line.target_version || '待接入' }}</p>
      <InventoryMetadataEditor :item="detail.line" kind="lines" :enabled="detail.capabilities.edit" @saved="metadataSaved" />
      <LinePathDiagram :path="detail.line.path" />
      <div v-if="!detail.line.path" class="registered-path">
        <section><h3>入口（{{ detail.line.ingress_count }}）</h3><ul class="endpoint-list"><li v-for="entry in detail.line.ingresses" :key="entry.id"><strong>{{ entry.server.name }} / {{ entry.name }}</strong><span>{{ entry.protocol_label }} · {{ entry.enabled ? '入口登记启用' : '入口登记停用' }} · {{ entry.server.enabled ? '服务器登记启用' : '服务器登记停用' }}</span></li></ul><p v-if="detail.line.ingresses_truncated" class="small muted">当前仅展示前 {{ detail.line.ingresses.length }} 个入口，登记总数为 {{ detail.line.ingress_count }}。</p></section>
        <div class="path-arrow" aria-hidden="true">↓</div>
        <section><h3>出口</h3><p><strong>{{ detail.line.egress.server.name }} / {{ detail.line.egress.name }}</strong></p><p class="small muted">{{ detail.line.egress.kind_label }} · {{ detail.line.egress.server.enabled ? '服务器登记启用' : '服务器登记停用' }}</p><p class="small muted">失败关闭登记：{{ detail.line.egress.fail_closed ? '已登记要求' : '未登记要求' }}；尚无实际整链验证证据。</p></section>
      </div>
      <template v-if="detail.nodes"><h2 class="nodes-title">相关节点与外部资源</h2><NodeResourceList :nodes="detail.nodes.items" :metadata-editable="detail.capabilities.edit" @saved="resourceMetadataSaved" /><p v-if="detail.nodes.truncated" class="small muted">当前显示 {{ detail.nodes.items.length }} 个，共 {{ detail.nodes.total }} 个相关资源。</p></template>
      <dl class="inventory-facts"><div><dt>线路登记</dt><dd>{{ detail.line.enabled ? '登记启用' : '登记停用' }}</dd></div><div><dt>端点登记</dt><dd>{{ detail.line.endpoint_registration === 'enabled' ? '登记均启用' : '含停用登记' }}</dd></div><div><dt>TCP / UDP / DNS</dt><dd>按下方具体目标与检测范围核对</dd></div><div><dt>整链状态</dt><dd>{{ verificationLabel(detail.line) }}</dd></div></dl>
      <InventoryChecks :checks="detail.checks" :actions="detail.check_actions" @completed="selected && showDetail(selected)" />
      <p class="quality-note">住宅或住宅 DNS 失败须关闭路径；上游没有 UDP 能力时须拒绝该路径 UDP。目前登记信息不能证明这些行为已经生效。</p>
    </template>
  </el-drawer>
</template>
<style scoped>
.target-version{overflow-wrap:anywhere}.nodes-title{font-size:18px}
.line-card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,340px),1fr));gap:18px}.line-view-controls{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:20px 0}.line-view-controls .el-button+.el-button{margin-left:6px}@media(max-width:520px){.line-view-controls{align-items:flex-start;flex-direction:column}}
.inventory-note{margin-bottom:20px}.association-filter{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:4px 0 18px}.line-entry+.line-entry{margin-top:5px}.registered-path{border:1px solid #e7ecf4;border-radius:14px;padding:18px;margin-top:20px}.registered-path h3{margin-top:0}.endpoint-list{list-style:none;padding:0;margin:12px 0}.endpoint-list li{padding:10px 0;display:flex;flex-direction:column;gap:5px}.endpoint-list span{font-size:13px;color:#75849b;line-height:1.6}.path-arrow{color:#75849b;padding:4px 0 18px;font-size:22px}.inventory-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin:24px 0}.inventory-facts dt{font-size:13px;color:#75849b}.inventory-facts dd{margin:6px 0 0;font-weight:600}.detail-retry{margin-top:14px}@media(max-width:520px){.inventory-facts{grid-template-columns:1fr}.association-filter{align-items:flex-start;flex-direction:column}}
</style>
