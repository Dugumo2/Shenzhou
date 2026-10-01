<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request, errorMessage } from '../api'
import { formatDate, formatGB } from '../display'
import type { Pagination, Service } from '../types'
import BillingDrawer from '../components/BillingDrawer.vue'
const items = ref<Service[]>([]), busy = ref(true), error = ref('')
const q = ref(''), state = ref('all'), page = ref(1), selected = ref<Service | null>(null)
const pagination = ref<Pagination>({ page: 1, page_size: 25, total: 0, pages: 0, has_next: false, has_previous: false })
const statuses = [{ id: 'all', label: '全部状态' }, { id: 'active', label: '已应用' }, { id: 'pending', label: '待开通' }, { id: 'disabled', label: '已停用' }, { id: 'suspended', label: '已暂停' }, { id: 'expired', label: '已到期' }, { id: 'enforcement_pending', label: '等待生效' }, { id: 'metering_gap', label: '用量待核算' }, { id: 'simulated', label: '隔离验证' }]
let generation = 0
async function load() {
  const current = ++generation
  busy.value = true; error.value = ''
  try { const data = await request<{ items: Service[]; pagination: Pagination }>('/admin/services?' + new URLSearchParams({ q: q.value.trim(), state: state.value, page: String(page.value), page_size: '25' })); if (current === generation) { items.value = data.items; pagination.value = data.pagination } }
  catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}
function search() { page.value = 1; void load() }
function changePage(value: number) { page.value = value; void load() }
onMounted(load)
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员</p><h1>订阅管理</h1><p class="muted">一份服务一行，查看额度与到期，独立调整重置时间。</p></div><el-button :loading="busy" @click="load">刷新</el-button></div>
  <section class="surface admin-panel"><form class="table-filters" @submit.prevent="search"><label class="visually-hidden" for="service-search">搜索账号或服务编号</label><el-input id="service-search" v-model="q" placeholder="搜索账号或服务编号" clearable /><el-select v-model="state" aria-label="服务状态" @change="search"><el-option v-for="status in statuses" :key="status.id" :value="status.id" :label="status.label" /></el-select><el-button type="primary" native-type="submit" :loading="busy">搜索</el-button></form>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
    <div class="table-scroll"><el-table v-loading="busy" :data="items" row-key="id" empty-text="没有符合条件的服务">
      <el-table-column label="用户 / 服务编号" min-width="185"><template #default="{ row }"><strong>{{ row.user?.username }}</strong><div class="small muted">神舟云 #{{ row.id.slice(0, 8) }}</div></template></el-table-column>
      <el-table-column label="总额度" min-width="120"><template #default="{ row }">{{ formatGB(row.quota_bytes) }}</template></el-table-column>
      <el-table-column label="已用" min-width="120"><template #default="{ row }">{{ formatGB(row.used_bytes, '暂无可靠统计') }}<div v-if="row.usage.quality !== 'measured' && row.used_bytes !== null" class="small muted">上次已确认</div></template></el-table-column>
      <el-table-column label="剩余" min-width="110"><template #default="{ row }">{{ formatGB(row.remaining_bytes, '待核算') }}</template></el-table-column>
      <el-table-column label="到期时间" min-width="175"><template #default="{ row }">{{ formatDate(row.expires_at) }}</template></el-table-column>
      <el-table-column label="状态" min-width="130"><template #default="{ row }"><el-tag :type="row.state === 'active' ? 'success' : 'info'">{{ row.status_label }}</el-tag></template></el-table-column>
      <el-table-column label="操作" min-width="115"><template #default="{ row }"><el-button v-if="row.actions?.billing" link type="primary" @click="selected = row">重置时间</el-button><span v-else class="small muted">暂不可调整</span></template></el-table-column>
    </el-table></div>
    <div class="table-bottom"><p class="small muted">开通、额度、续期与授权将在执行能力接通后分别提供。</p><el-pagination :current-page="pagination.page" :page-size="pagination.page_size" :total="pagination.total" layout="total, prev, pager, next" @current-change="changePage" /></div>
  </section>
  <BillingDrawer :service="selected" @close="selected = null" @saved="load" />
</template>
