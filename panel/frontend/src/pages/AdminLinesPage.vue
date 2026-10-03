<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage, request } from '../api'
import type { InventoryList, LineInventory, LineInventoryDetail } from '../inventoryTypes'
import type { Pagination } from '../types'

const route = useRoute(), router = useRouter()
const items = ref<LineInventory[]>([]), busy = ref(true), error = ref('')
const q = ref(''), status = ref('all'), serverId = ref(''), page = ref(1)
const pagination = ref<Pagination>({ page: 1, page_size: 25, total: 0, pages: 0, has_next: false, has_previous: false })
const selected = ref<LineInventory | null>(null), detail = ref<LineInventoryDetail | null>(null)
const detailBusy = ref(false), detailError = ref('')
const statuses = [{ id: 'all', label: '全部登记状态' }, { id: 'enabled', label: '登记启用' }, { id: 'disabled', label: '登记停用' }]
let generation = 0, detailGeneration = 0
function closeDetail() { detailGeneration++; selected.value = null; detail.value = null; detailBusy.value = false; detailError.value = '' }
async function load() {
  const current = ++generation
  busy.value = true; error.value = ''; closeDetail()
  try {
    const params = new URLSearchParams({ q: q.value.trim(), status: status.value, page: String(page.value), page_size: '25' })
    if (serverId.value) params.set('server_id', serverId.value)
    const data = await request<InventoryList<LineInventory>>('/admin/lines?' + params)
    if (current === generation) { items.value = data.items; pagination.value = data.pagination }
  } catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}
async function showDetail(line: LineInventory) {
  const current = ++detailGeneration
  selected.value = line; detail.value = null; detailError.value = ''; detailBusy.value = true
  try {
    const data = await request<LineInventoryDetail>('/admin/lines/' + encodeURIComponent(line.id))
    if (current === detailGeneration) detail.value = data
  } catch (e) { if (current === detailGeneration) detailError.value = errorMessage(e) }
  finally { if (current === detailGeneration) detailBusy.value = false }
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
watch(() => route.query, query => {
  q.value = typeof query.q === 'string' ? query.q : ''
  status.value = typeof query.status === 'string' && statuses.some(item => item.id === query.status) ? query.status : 'all'
  serverId.value = typeof query.server_id === 'string' ? query.server_id : ''
  const requested = typeof query.page === 'string' ? Number(query.page) : 1
  page.value = Number.isSafeInteger(requested) && requested > 0 ? requested : 1
  void load()
}, { immediate: true })
onUnmounted(() => { generation++; closeDetail() })
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员工作区</p><h1>线路</h1><p class="muted">分别查看登记的入口、协议与出口关联。</p></div><el-button :loading="busy" @click="load">刷新登记</el-button></div>
  <el-alert title="当前只展示已有入口与出口关联；完整上下游编排、路径编辑与真实整链检测尚未接通。" type="info" show-icon :closable="false" class="inventory-note" />
  <section class="surface admin-panel">
    <form class="table-filters" @submit.prevent="search"><label class="visually-hidden" for="line-search">搜索线路、端点、协议或服务器</label><el-input id="line-search" v-model="q" placeholder="搜索线路、端点、协议或服务器" clearable /><el-select v-model="status" aria-label="线路登记状态" @change="search"><el-option v-for="item in statuses" :key="item.id" :value="item.id" :label="item.label" /></el-select><el-button type="primary" native-type="submit" :loading="busy">搜索</el-button></form>
    <div v-if="serverId" class="association-filter"><span class="small muted">按服务器 #{{ serverId.slice(0, 8) }} 查看关联线路（含主入口、附加入口和出口）。</span><el-button link type="primary" @click="clearServer">清除关联筛选</el-button></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
    <template v-else><div class="table-scroll"><el-table v-loading="busy" :data="items" row-key="id" empty-text="没有符合条件的线路登记">
      <el-table-column label="线路" min-width="160"><template #default="{ row }"><el-button link type="primary" @click="showDetail(row)">{{ row.name }}</el-button><div class="small muted">#{{ row.id.slice(0, 8) }}</div></template></el-table-column>
      <el-table-column label="入口服务器 / 协议" min-width="230"><template #default="{ row }"><div v-for="entry in row.ingresses.slice(0, 3)" :key="entry.id" class="line-entry">{{ entry.server.name }} <span class="small muted">/ {{ entry.protocol_label }}</span></div><div v-if="row.ingress_count > 3" class="small muted">共 {{ row.ingress_count }} 个入口，详情查看。</div></template></el-table-column>
      <el-table-column label="出口服务器 / 类型" min-width="190"><template #default="{ row }">{{ row.egress.server.name }}<div class="small muted">{{ row.egress.kind_label }} · {{ row.egress.name }}</div></template></el-table-column>
      <el-table-column label="线路登记" min-width="115"><template #default="{ row }"><el-tag type="info">{{ row.enabled ? '登记启用' : '登记停用' }}</el-tag></template></el-table-column>
      <el-table-column label="端点登记" min-width="145"><template #default="{ row }"><span :class="{ muted: row.endpoint_registration === 'enabled' }">{{ row.endpoint_registration === 'enabled' ? '登记均启用' : '含停用登记' }}</span></template></el-table-column>
      <el-table-column label="整链检测" min-width="110"><template #default><span class="muted">尚未检测</span></template></el-table-column>
      <el-table-column label="操作" width="100"><template #default="{ row }"><el-button link type="primary" @click="showDetail(row)">查看详情</el-button></template></el-table-column>
    </el-table></div><div class="table-bottom"><p class="small muted">登记状态与实际可用性分开，路径编辑与发布尚未接通。</p><el-pagination :current-page="pagination.page" :page-size="pagination.page_size" :total="pagination.total" layout="total, prev, pager, next" @current-change="changePage" /></div></template>
  </section>
  <el-drawer :model-value="selected !== null" :title="selected?.name || '线路详情'" size="min(740px, 100vw)" @close="closeDetail">
    <el-skeleton v-if="detailBusy" :rows="7" animated />
    <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" show-icon />
    <el-button v-if="detailError && selected" class="detail-retry" @click="showDetail(selected)">重新读取详情</el-button>
    <template v-if="detail">
      <p class="small muted">编号 {{ detail.line.id }}</p>
      <div class="section-title"><h2>入口 → 出口登记</h2><el-tag type="info">尚未检测</el-tag></div>
      <p class="quality-note">现有记录表示入口与出口的关联，尚未描述中间跳段、独立核心实例、TCP/UDP 能力或 DNS 路径。</p>
      <div class="registered-path">
        <section><h3>入口（{{ detail.line.ingress_count }}）</h3><ul class="endpoint-list"><li v-for="entry in detail.line.ingresses" :key="entry.id"><strong>{{ entry.server.name }} / {{ entry.name }}</strong><span>{{ entry.protocol_label }} · {{ entry.enabled ? '入口登记启用' : '入口登记停用' }} · {{ entry.server.enabled ? '服务器登记启用' : '服务器登记停用' }}</span></li></ul><p v-if="detail.line.ingresses_truncated" class="small muted">当前仅展示前 {{ detail.line.ingresses.length }} 个入口，登记总数为 {{ detail.line.ingress_count }}。</p></section>
        <div class="path-arrow" aria-hidden="true">↓</div>
        <section><h3>出口</h3><p><strong>{{ detail.line.egress.server.name }} / {{ detail.line.egress.name }}</strong></p><p class="small muted">{{ detail.line.egress.kind_label }} · {{ detail.line.egress.server.enabled ? '服务器登记启用' : '服务器登记停用' }}</p><p class="small muted">失败关闭登记：{{ detail.line.egress.fail_closed ? '已登记要求' : '未登记要求' }}；尚无实际整链验证证据。</p></section>
      </div>
      <dl class="inventory-facts"><div><dt>线路登记</dt><dd>{{ detail.line.enabled ? '登记启用' : '登记停用' }}</dd></div><div><dt>端点登记</dt><dd>{{ detail.line.endpoint_registration === 'enabled' ? '登记均启用' : '含停用登记' }}</dd></div><div><dt>TCP / UDP / DNS</dt><dd>待完整路径验证</dd></div><div><dt>整链检测时间</dt><dd>暂无检测记录</dd></div></dl>
      <p class="quality-note">住宅或住宅 DNS 失败须关闭路径；上游没有 UDP 能力时须拒绝该路径 UDP。目前登记信息不能证明这些行为已经生效。</p>
    </template>
  </el-drawer>
</template>
<style scoped>
.inventory-note{margin-bottom:20px}.association-filter{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:4px 0 18px}.line-entry+.line-entry{margin-top:5px}.registered-path{border:1px solid #e7ecf4;border-radius:14px;padding:18px;margin-top:20px}.registered-path h3{margin-top:0}.endpoint-list{list-style:none;padding:0;margin:12px 0}.endpoint-list li{padding:10px 0;display:flex;flex-direction:column;gap:5px}.endpoint-list span{font-size:13px;color:#75849b;line-height:1.6}.path-arrow{color:#75849b;padding:4px 0 18px;font-size:22px}.inventory-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin:24px 0}.inventory-facts dt{font-size:13px;color:#75849b}.inventory-facts dd{margin:6px 0 0;font-weight:600}.detail-retry{margin-top:14px}@media(max-width:520px){.inventory-facts{grid-template-columns:1fr}.association-filter{align-items:flex-start;flex-direction:column}}
</style>
