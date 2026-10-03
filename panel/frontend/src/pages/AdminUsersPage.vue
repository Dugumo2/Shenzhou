<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage, request } from '../api'
import type { AdminUser, AdminUsers } from '../adminTypes'
import type { Pagination } from '../types'

const route = useRoute(), router = useRouter()
const items = ref<AdminUser[]>([]), busy = ref(true), error = ref('')
const q = ref(''), status = ref('all'), page = ref(1)
const pagination = ref<Pagination>({ page: 1, page_size: 25, total: 0, pages: 0, has_next: false, has_previous: false })
const statuses = [{ id: 'all', label: '全部账号状态' }, { id: 'active', label: '已启用账号' }, { id: 'disabled', label: '已停用账号' }]
let generation = 0
async function load() {
  const current = ++generation
  busy.value = true; error.value = ''
  try {
    const data = await request<AdminUsers>('/admin/users?' + new URLSearchParams({ q: q.value.trim(), status: status.value, page: String(page.value), page_size: '25' }))
    if (current === generation) { items.value = data.items; pagination.value = data.pagination }
  } catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}
async function updateFilters(nextPage: number) {
  page.value = nextPage
  await router.replace({ path: '/admin/users', query: { ...(q.value.trim() ? { q: q.value.trim() } : {}), ...(status.value !== 'all' ? { status: status.value } : {}), ...(page.value > 1 ? { page: String(page.value) } : {}) } })
}
function search() { void updateFilters(1) }
function changePage(value: number) { void updateFilters(value) }
watch(() => route.query, query => {
  q.value = typeof query.q === 'string' ? query.q : ''
  status.value = typeof query.status === 'string' && statuses.some(item => item.id === query.status) ? query.status : 'all'
  const requestedPage = typeof query.page === 'string' ? Number(query.page) : 1
  page.value = Number.isSafeInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1
  void load()
}, { immediate: true })
onUnmounted(() => { generation++ })
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员工作区</p><h1>用户管理</h1><p class="muted">查找账号，进入具体用户查看分配的服务。</p></div><el-button :loading="busy" @click="load">刷新</el-button></div>
  <section class="surface admin-panel">
    <form class="table-filters" @submit.prevent="search"><label class="visually-hidden" for="user-search">搜索用户名</label><el-input id="user-search" v-model="q" placeholder="搜索用户名" clearable /><el-select v-model="status" aria-label="账号状态" @change="search"><el-option v-for="item in statuses" :key="item.id" :value="item.id" :label="item.label" /></el-select><el-button type="primary" native-type="submit" :loading="busy">搜索</el-button></form>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
    <template v-else><div class="table-scroll admin-users-table"><el-table v-loading="busy" :data="items" row-key="id" empty-text="没有符合条件的用户">
      <el-table-column label="用户" min-width="210"><template #default="{ row }"><RouterLink :to="{ path: '/admin/users/' + encodeURIComponent(row.id), query: route.query }" class="table-user-link">{{ row.username }}</RouterLink><div class="small muted">账号 #{{ row.id }}</div></template></el-table-column>
      <el-table-column label="账号状态" min-width="130"><template #default="{ row }"><el-tag :type="row.is_active ? 'success' : 'info'">{{ row.is_active ? '已启用' : '已停用' }}</el-tag></template></el-table-column>
      <el-table-column label="角色" min-width="115"><template #default="{ row }">{{ row.is_staff ? '管理员' : '普通用户' }}</template></el-table-column>
      <el-table-column label="已分配服务" min-width="130"><template #default="{ row }">{{ row.service_count.toLocaleString('zh-CN') }}</template></el-table-column>
      <el-table-column label="来源归属" min-width="130"><template #default="{ row }"><el-tag v-if="row.mapping_required" type="warning">待核对</el-tag><span v-else class="small muted">无待核对标记</span></template></el-table-column>
      <el-table-column label="操作" min-width="120"><template #default="{ row }"><RouterLink :to="{ path: '/admin/users/' + encodeURIComponent(row.id), query: route.query }" class="small">查看用户 →</RouterLink></template></el-table-column>
    </el-table></div>
    <div class="table-bottom"><p class="small muted">账号状态与服务状态分别展示；服务开通能力尚未接通。</p><el-pagination :current-page="pagination.page" :page-size="pagination.page_size" :total="pagination.total" layout="total, prev, pager, next" @current-change="changePage" /></div></template>
  </section>
</template>
