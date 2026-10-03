<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { errorMessage, request } from '../api'
import { formatDate } from '../display'
import type { AdminOverview } from '../adminTypes'

const overview = ref<AdminOverview | null>(null)
const busy = ref(true), error = ref('')
let generation = 0
function count(value: number | null): string {
  return value !== null && Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('zh-CN') : '尚未汇总'
}
async function load() {
  const current = ++generation
  busy.value = true; error.value = ''
  try { const data = await request<AdminOverview>('/admin/overview'); if (current === generation) overview.value = data }
  catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}
onMounted(load)
onUnmounted(() => { generation++ })
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员工作区</p><h1>管理总览</h1><p class="muted">查看已登记的用户、服务与需要核对的事项。</p></div><el-button :loading="busy" @click="load">刷新</el-button></div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
  <el-skeleton v-if="busy && !overview" :rows="8" animated />
  <template v-if="overview">
    <p class="small muted overview-updated">汇总时间：{{ formatDate(overview.generated_at) }}<span v-if="overview.environment?.is_demo"> · 演示数据</span><span v-if="error"> · 本次刷新失败，以下保留上次结果</span></p>
    <section class="surface overview-section"><div class="section-title"><h2>用户</h2><RouterLink to="/admin/users" class="small">进入用户管理 →</RouterLink></div>
      <dl class="overview-metrics"><div><dt>用户总数</dt><dd>{{ count(overview.users.total) }}</dd></div><div><dt>已启用账号</dt><dd>{{ count(overview.users.active) }}</dd></div><div><dt>已停用账号</dt><dd>{{ count(overview.users.disabled) }}</dd></div></dl>
    </section>
    <section class="surface overview-section"><div class="section-title"><h2>服务与核对事项</h2><RouterLink to="/admin/services" class="small">进入订阅管理 →</RouterLink></div>
      <dl class="overview-metrics"><div><dt>服务总数</dt><dd>{{ count(overview.services.total) }}</dd><p>按业务服务计数，不按软件格式计数。</p></div><div><dt>已到期服务</dt><dd>{{ count(overview.services.expired) }}</dd><p>依据已登记的到期时间。</p></div><div><dt>待核对用户</dt><dd>{{ count(overview.services.mapping_required) }}</dd><p>旧来源归属需要核对的用户数。</p></div><div><dt>已记录计量缺口</dt><dd>{{ count(overview.services.recorded_metering_gaps) }}</dd><p>仅统计数据库已保存的缺口标记。</p></div><div><dt>计量质量汇总</dt><dd :class="{ 'metric-text': overview.services.statistics_incomplete === null }">{{ count(overview.services.statistics_incomplete) }}</dd><p>缺口、过期与未知统计的完整汇总。</p></div></dl>
      <p class="quality-note">以上是管理库存和已记录状态。账号启用、服务登记或缺口数为零，都不能证明节点在线或计量采样完整。</p>
    </section>
    <section v-if="overview.limitations.length" class="surface overview-section"><h2>当前数据范围</h2><ul class="overview-limitations"><li v-for="(item, index) in overview.limitations" :key="index">{{ item }}</li></ul></section>
  </template>
</template>
