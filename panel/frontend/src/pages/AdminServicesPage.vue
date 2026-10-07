<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { auth } from '../auth'
import { retainResourceList } from '../resourceSnapshot'
import { useSnapshotRequest } from '../useSnapshotRequest'
import { useSessionIdentity } from '../useSessionIdentity'
import { formatDate, formatGB } from '../display'
import type { Pagination, Service } from '../types'
import BillingDrawer from '../components/BillingDrawer.vue'
import RefreshControl from '../components/RefreshControl.vue'
const snapshot = useSnapshotRequest<{ items: Service[]; pagination: Pagination }>({ reconcile: retainResourceList })
const { busy, error, initialLoading, lastReadAt } = snapshot
const identity = useSessionIdentity(() => auth.session)
const items = computed(() => snapshot.data.value?.items ?? [])
const q = ref(''), state = ref('all'), page = ref(1)
const selectedId = ref<string | null>(null)
const selected = computed(() => items.value.find(item => item.id === selectedId.value && item.actions?.billing) ?? null)
const pagination = computed<Pagination>(() => snapshot.data.value?.pagination ?? { page: 1, page_size: 25, total: 0, pages: 0, has_next: false, has_previous: false })
const statuses = [{ id: 'all', label: '全部状态' }, { id: 'verification_required', label: '资源待核验' }, { id: 'mapping_required', label: '资料待核对' }, { id: 'account_disabled', label: '账号已停用' }, { id: 'exhausted', label: '额度已用完' }, { id: 'active', label: '已应用且统计正常' }, { id: 'pending', label: '待开通' }, { id: 'disabled', label: '已停用' }, { id: 'suspended', label: '已暂停' }, { id: 'expired', label: '已到期' }, { id: 'enforcement_pending', label: '等待生效' }, { id: 'metering_gap', label: '计量存在缺口' }, { id: 'simulated', label: '隔离验证' }]
function load() {
  if (!auth.session?.authenticated || !auth.session.user?.is_staff) { snapshot.reset(); return }
  return snapshot.load('/admin/services?' + new URLSearchParams({ q: q.value.trim(), state: state.value, page: String(page.value), page_size: '25' }), identity.value)
}
function search() { page.value = 1; void load() }
function changePage(value: number) { page.value = value; void load() }
watch(items, () => { if (!selected.value) selectedId.value = null }, { flush: 'sync' })
watch(identity, () => { selectedId.value = null; snapshot.reset(); void load() }, { immediate: true, flush: 'sync' })
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员</p><h1>订阅管理</h1><p class="muted">一份服务一行，查看统一套餐额度与到期时间。</p></div><RefreshControl :loading="busy" :error="error" :last-read-at="lastReadAt" @refresh="load" /></div>
  <section class="surface admin-panel"><form class="table-filters" @submit.prevent="search"><label class="visually-hidden" for="service-search">搜索账号或服务编号</label><el-input id="service-search" v-model="q" placeholder="搜索账号或服务编号" clearable /><el-select v-model="state" aria-label="服务状态" @change="search"><el-option v-for="status in statuses" :key="status.id" :value="status.id" :label="status.label" /></el-select><el-button type="primary" native-type="submit" :loading="busy">搜索</el-button></form>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
    <div class="table-scroll"><el-table v-loading="initialLoading" :data="items" row-key="id" empty-text="没有符合条件的服务">
      <el-table-column label="用户 / 服务编号" min-width="185"><template #default="{ row }"><strong>{{ row.user?.username }}</strong><div class="small muted">神舟云 #{{ row.id.slice(0, 8) }}</div><div class="small muted">{{ row.source_type === 'p8' ? '原订阅资源' : row.source_type === 'membership' ? '会员登记' : '服务权益' }}</div></template></el-table-column>
      <el-table-column label="总额度" min-width="120"><template #default="{ row }">{{ formatGB(row.quota_bytes) }}<div v-if="row.quota_state === 'configured'" class="small muted">待应用</div></template></el-table-column>
      <el-table-column label="已用" min-width="130"><template #default="{ row }">{{ formatGB(row.used_bytes, '暂无可靠统计') }}<div v-if="row.usage.quality !== 'measured' && row.used_bytes !== null" class="small muted">上次已确认</div></template></el-table-column>
      <el-table-column label="剩余" min-width="110"><template #default="{ row }">{{ formatGB(row.remaining_bytes, '待核算') }}</template></el-table-column>
      <el-table-column label="到期时间" min-width="175"><template #default="{ row }">{{ formatDate(row.expires_at) }}</template></el-table-column>
      <el-table-column label="状态" min-width="130"><template #default="{ row }"><el-tag :type="row.business_state === 'active' ? 'success' : 'info'">{{ row.status_label }}</el-tag></template></el-table-column>
      <el-table-column label="操作" min-width="130"><template #default="{ row }"><el-button v-if="row.actions?.billing" link type="primary" @click="selectedId = row.id">重置时间</el-button><span v-if="!row.actions?.billing" class="small muted">暂不可调整</span></template></el-table-column>
    </el-table></div>
    <div class="table-bottom"><p class="small muted">开通、额度、续期与授权将在执行能力接通后分别提供。</p><el-pagination :current-page="pagination.page" :page-size="pagination.page_size" :total="pagination.total" layout="total, prev, pager, next" @current-change="changePage" /></div>
  </section>
  <BillingDrawer :service="selected" @close="selectedId = null" @saved="load" />
</template>
