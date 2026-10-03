<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage, request } from '../api'
import { formatDate } from '../display'
import type { InventoryList, ServerInventory, ServerInventoryDetail } from '../inventoryTypes'
import type { Pagination } from '../types'

const route = useRoute(), router = useRouter()
const items = ref<ServerInventory[]>([]), busy = ref(true), error = ref('')
const q = ref(''), status = ref('all'), page = ref(1)
const pagination = ref<Pagination>({ page: 1, page_size: 25, total: 0, pages: 0, has_next: false, has_previous: false })
const selected = ref<ServerInventory | null>(null), detail = ref<ServerInventoryDetail | null>(null)
const detailBusy = ref(false), detailError = ref('')
const statuses = [{ id: 'all', label: '全部登记状态' }, { id: 'enabled', label: '登记启用' }, { id: 'disabled', label: '登记停用' }]
let generation = 0, detailGeneration = 0
function closeDetail() { detailGeneration++; selected.value = null; detail.value = null; detailBusy.value = false; detailError.value = '' }
async function load() {
  const current = ++generation
  busy.value = true; error.value = ''; closeDetail()
  try {
    const data = await request<InventoryList<ServerInventory>>('/admin/servers?' + new URLSearchParams({ q: q.value.trim(), status: status.value, page: String(page.value), page_size: '25' }))
    if (current === generation) { items.value = data.items; pagination.value = data.pagination }
  } catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}
async function showDetail(server: ServerInventory) {
  const current = ++detailGeneration
  selected.value = server; detail.value = null; detailError.value = ''; detailBusy.value = true
  try {
    const data = await request<ServerInventoryDetail>('/admin/servers/' + encodeURIComponent(server.id))
    if (current === detailGeneration) detail.value = data
  } catch (e) { if (current === detailGeneration) detailError.value = errorMessage(e) }
  finally { if (current === detailGeneration) detailBusy.value = false }
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
watch(() => route.query, query => {
  q.value = typeof query.q === 'string' ? query.q : ''
  status.value = typeof query.status === 'string' && statuses.some(item => item.id === query.status) ? query.status : 'all'
  const requested = typeof query.page === 'string' ? Number(query.page) : 1
  page.value = Number.isSafeInteger(requested) && requested > 0 ? requested : 1
  void load()
}, { immediate: true })
onUnmounted(() => { generation++; closeDetail() })
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员工作区</p><h1>服务器</h1><p class="muted">查看登记的机器、适配器与关联端点。</p></div><el-button :loading="busy" @click="load">刷新登记</el-button></div>
  <el-alert title="监控尚未接入：登记启用和最后记录都不能证明服务器在线。刷新只读取登记资料。" type="info" show-icon :closable="false" class="inventory-note" />
  <section class="surface admin-panel">
    <form class="table-filters" @submit.prevent="search"><label class="visually-hidden" for="server-search">搜索服务器或适配器</label><el-input id="server-search" v-model="q" placeholder="搜索服务器或适配器" clearable /><el-select v-model="status" aria-label="服务器登记状态" @change="search"><el-option v-for="item in statuses" :key="item.id" :value="item.id" :label="item.label" /></el-select><el-button type="primary" native-type="submit" :loading="busy">搜索</el-button></form>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
    <template v-else><div class="table-scroll"><el-table v-loading="busy" :data="items" row-key="id" empty-text="没有符合条件的服务器登记">
      <el-table-column label="服务器" min-width="180"><template #default="{ row }"><el-button link type="primary" @click="showDetail(row)">{{ row.name }}</el-button><div class="small muted">#{{ row.id.slice(0, 8) }}</div></template></el-table-column>
      <el-table-column label="登记状态" min-width="120"><template #default="{ row }"><el-tag type="info">{{ row.enabled ? '登记启用' : '登记停用' }}</el-tag></template></el-table-column>
      <el-table-column label="监控状态" min-width="140"><template #default><span class="muted">未接入监控</span></template></el-table-column>
      <el-table-column label="登记适配器" min-width="140"><template #default="{ row }">{{ row.adapter === 'unconfigured' ? '未配置' : row.adapter }}</template></el-table-column>
      <el-table-column label="登记端点" min-width="140"><template #default="{ row }">{{ row.ingress_count }} 入口 · {{ row.egress_count }} 出口</template></el-table-column>
      <el-table-column label="最后记录" min-width="175"><template #default="{ row }"><span class="small">{{ recordedAt(row.last_seen_at) }}</span></template></el-table-column>
      <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" @click="showDetail(row)">查看详情</el-button></template></el-table-column>
    </el-table></div><div class="table-bottom"><p class="small muted">仅查询；机器监控、核心管理与检测尚未接通。</p><el-pagination :current-page="pagination.page" :page-size="pagination.page_size" :total="pagination.total" layout="total, prev, pager, next" @current-change="changePage" /></div></template>
  </section>
  <el-drawer :model-value="selected !== null" :title="selected?.name || '服务器详情'" size="min(720px, 100vw)" @close="closeDetail">
    <el-skeleton v-if="detailBusy" :rows="7" animated />
    <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" show-icon />
    <el-button v-if="detailError && selected" class="detail-retry" @click="showDetail(selected)">重新读取详情</el-button>
    <template v-if="detail">
      <p class="small muted">编号 {{ detail.server.id }}</p>
      <div class="section-title"><h2>状态与指标</h2><el-tag type="info">未接入监控</el-tag></div>
      <dl class="inventory-facts"><div><dt>登记状态</dt><dd>{{ detail.server.enabled ? '登记启用' : '登记停用' }}</dd></div><div><dt>最后记录</dt><dd>{{ recordedAt(detail.server.last_seen_at) }}</dd></div><div><dt>CPU / 内存 / 磁盘</dt><dd>未接入</dd></div><div><dt>网速 / 整机流量</dt><dd>未接入</dd></div><div><dt>实际核心版本</dt><dd>未接入</dd></div><div><dt>指标采样时间</dt><dd>暂无可信样本</dd></div></dl>
      <p class="quality-note">{{ detail.server.adapter === 'unconfigured' ? '尚未登记适配器。' : `已登记 ${detail.server.adapter} 适配器。` }} 适配器名称不证明核心已安装、正在运行或版本已核验；最后记录也不是健康检测。</p>
      <div class="section-title"><h2>核心与端点登记</h2><RouterLink :to="{ path: '/admin/lines', query: { server_id: detail.server.id } }" @click="closeDetail">查看关联线路 →</RouterLink></div>
      <p class="small muted">独立核心实例尚未建模，以下为现有入口与出口登记。</p>
      <h3>入口（{{ detail.ingresses.total }}）</h3>
      <ul v-if="detail.ingresses.items.length" class="endpoint-list"><li v-for="entry in detail.ingresses.items" :key="entry.id"><span>{{ entry.name }}</span><span class="small muted">{{ entry.protocol_label }} · {{ entry.enabled ? '登记启用' : '登记停用' }}</span></li></ul><p v-else class="muted">尚无入口登记。</p>
      <p v-if="detail.ingresses.truncated" class="small muted">当前仅展示前 {{ detail.ingresses.items.length }} 个入口，登记总数为 {{ detail.ingresses.total }}。</p>
      <h3>出口（{{ detail.egresses.total }}）</h3>
      <ul v-if="detail.egresses.items.length" class="endpoint-list"><li v-for="entry in detail.egresses.items" :key="entry.id"><span>{{ entry.name }}</span><span class="small muted">{{ entry.kind_label }}</span></li></ul><p v-else class="muted">尚无出口登记。</p>
      <p v-if="detail.egresses.truncated" class="small muted">当前仅展示前 {{ detail.egresses.items.length }} 个出口，登记总数为 {{ detail.egresses.total }}。</p>
      <div class="inventory-pending"><h3>检测、流量与账单、备份</h3><p>暂无已接入的检测记录、供应商账单或备份回执。核心编辑、升级、远程检测和备份操作尚未接通。</p></div>
    </template>
  </el-drawer>
</template>
<style scoped>
.inventory-note{margin-bottom:20px}.inventory-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin:24px 0}.inventory-facts dt{font-size:13px;color:#75849b}.inventory-facts dd{margin:6px 0 0;font-weight:600}.endpoint-list{list-style:none;padding:0;margin:12px 0 24px}.endpoint-list li{display:flex;justify-content:space-between;gap:14px;padding:11px 0;border-bottom:1px solid #e7ecf4}.inventory-pending{margin-top:28px;padding-top:16px;border-top:1px solid #e7ecf4;color:#75849b;line-height:1.7}.detail-retry{margin-top:14px}@media(max-width:520px){.inventory-facts{grid-template-columns:1fr}.endpoint-list li{flex-direction:column;gap:4px}}
</style>
