<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ApiError, errorMessage, request } from '../api'
import { formatDate } from '../display'
import type { CoreControlDetail, CoreCandidate, CoreControlJob } from '../coreControlTypes'
import RefreshControl from './RefreshControl.vue'

const props = defineProps<{ coreId: string }>()
const detail = ref<CoreControlDetail | null>(null), selectedId = ref(''), level = ref('')
const reading = ref(false), writing = ref(false), readError = ref(''), writeError = ref(''), notice = ref('')
const lastReadAt = ref<string | null>(null), uncertain = ref(false), permissionDenied = ref(false), stale = ref(true)
const impactConfirmed = ref(false)
let generation = 0
let readController: AbortController | null = null, writeController: AbortController | null = null
const selected = computed(() => detail.value?.candidates.find(item => item.id === selectedId.value) || null)
const activeJobs = computed(() => detail.value?.jobs.some(job => ['queued', 'running', 'unknown'].includes(job.status)) || false)
const ready = computed(() => !!detail.value && detail.value.owner_state === 'verified' && detail.value.binding.available
  && detail.value.write_enabled && !detail.value.busy && !activeJobs.value && !reading.value && !writing.value
  && !uncertain.value && !permissionDenied.value && !stale.value)
const canCreate = computed(() => ready.value && detail.value?.capabilities.create_candidate
  && detail.value.capabilities.log_levels.some(option => option === level.value))
const canCheck = computed(() => ready.value && !!selected.value && selected.value.base_revision === detail.value?.revision
  && selected.value.capabilities.check)
const canApply = computed(() => ready.value && !!selected.value && selected.value.state === 'checked'
  && !!selected.value.checked_at && selected.value.base_revision === detail.value?.revision && selected.value.capabilities.apply)
const canRollback = computed(() => ready.value && !!selected.value && selected.value.state === 'applied'
  && selected.value.id === detail.value?.current_applied_candidate_id && selected.value.capabilities.rollback)
const limitation = computed(() => {
  if (permissionDenied.value) return '当前身份无权操作核心，请重新核对管理员权限。'
  if (uncertain.value) return '请求回执未知，暂不再次执行。请读取任务状态并核对原请求；不能因超时再次重启或回滚。'
  if (!detail.value) return ''
  if (detail.value.owner_state !== 'verified') return '运行控制尚未接管；配置所有者尚未核实，当前仅可读取登记和任务资料。'
  if (!detail.value.binding.available) return detail.value.binding.reason === 'server_disabled'
    ? '所属服务器已停用，当前不能执行核心变更。' : '固定配置来源与执行绑定尚未就绪，当前不能执行核心变更。'
  if (!detail.value.write_enabled) return '此环境尚未开放核心配置写入。'
  if (detail.value.jobs.some(job => job.status === 'unknown')) return '存在结果未知的任务，只能先读取状态，不能重新执行或自动回滚。'
  if (detail.value.busy || activeJobs.value) return '此核心已有任务等待完成，请更新执行状态后继续。'
  if (stale.value) return '当前版本需要重新读取，核对后再操作。'
  return ''
})
const candidateLabels: Record<string, string> = { prepared: '待检查', checked: '检查通过', check_failed: '检查未通过', applied: '已应用', rolled_back: '已回滚' }
const jobLabels: Record<string, string> = { queued: '等待执行', running: '执行中', succeeded: '执行成功', failed: '执行失败', unknown: '结果未知' }
const actionLabels: Record<string, string> = { check: '检查', apply: '应用', rollback: '回滚' }
const errorLabels: Record<string, string> = {
  check_failed: '候选未通过核心检查，请核对后再检查。', job_expired: '任务已过期，请更新版本并重新核对候选。',
  revision_conflict: '核心修订已变化，请读取当前版本后重新生成候选。', source_changed: '配置来源已变化，请重新生成候选。',
  identity_changed: '运行身份已变化，请重新生成候选并核对影响。', permission_revoked: '管理员权限已撤销，任务没有继续执行。',
  control_disabled: '此环境的核心执行已关闭。', stale_check: '原检查已失效，请重新检查当前候选。',
  executor_interrupted: '执行回执中断，请只读核对真实状态，勿重复执行。', receipt_mismatch: '执行回执与本次目标不一致，需先核对真实状态。',
}
const path = () => '/admin/cores/' + encodeURIComponent(props.coreId) + '/control'

async function refresh() {
  if (reading.value || writing.value) return
  const current = generation
  const controller = new AbortController(); readController = controller
  const timer = setTimeout(() => controller.abort(), 15000)
  reading.value = true; readError.value = ''
  try {
    const result = await request<CoreControlDetail>(path(), 'GET', undefined, controller.signal)
    if (current !== generation) return
    if (result.core_id !== props.coreId) throw new ApiError(0, 'TARGET_MISMATCH', '读取目标不一致，请重新核对核心。')
    detail.value = result; stale.value = false; permissionDenied.value = false
    if (!result.candidates.some(item => item.id === selectedId.value)) selectedId.value = result.candidates[0]?.id || ''
    if (!result.capabilities.log_levels.some(option => option === level.value)) level.value = result.capabilities.log_levels[0] || ''
    lastReadAt.value = new Date().toISOString()
  } catch (error) {
    if (current !== generation) return
    readError.value = controller.signal.aborted ? '读取超时，已保留上次内容。' : errorMessage(error)
    stale.value = true
    if (error instanceof ApiError && [401, 403].includes(error.status)) permissionDenied.value = true
  } finally {
    clearTimeout(timer)
    if (current === generation) { reading.value = false; readController = null }
  }
}

async function submit(action: 'candidate' | 'check' | 'apply' | 'rollback') {
  if (action === 'candidate' ? !canCreate.value : action === 'check' ? !canCheck.value
    : action === 'apply' ? !canApply.value || !impactConfirmed.value : !canRollback.value || !impactConfirmed.value) return
  const current = generation, candidateId = selected.value?.id
  const body = { expected_revision: detail.value!.revision, idempotency_key: crypto.randomUUID(),
    ...(action === 'candidate' ? { patch: { log: { level: level.value } } } : {}) }
  const endpoint = action === 'candidate' ? path() + '/candidates' : path() + '/candidates/' + encodeURIComponent(candidateId!) + '/' + action
  const controller = new AbortController(); writeController = controller
  const timer = setTimeout(() => controller.abort(), 15000)
  writing.value = true; writeError.value = ''; notice.value = ''; impactConfirmed.value = false
  try {
    const result = await request<{ candidate?: CoreCandidate; job?: CoreControlJob; replayed: boolean }>(endpoint, 'POST', body, controller.signal)
    if (current !== generation) return
    if (action === 'candidate' && result.candidate?.core_id === props.coreId) {
      detail.value!.candidates = [result.candidate, ...detail.value!.candidates.filter(item => item.id !== result.candidate!.id)]
      selectedId.value = result.candidate.id
      notice.value = '候选已生成，请核对差异和影响，再单独检查。'
    } else if (action !== 'candidate' && result.job?.core_id === props.coreId && result.job.candidate_id === candidateId && result.job.action === action) {
      detail.value!.jobs = [result.job, ...detail.value!.jobs.filter(item => item.id !== result.job!.id)]
      detail.value!.busy = ['queued', 'running', 'unknown'].includes(result.job.status)
      notice.value = actionLabels[action] + '任务已提交；实际结果以执行回执为准。'
    } else {
      uncertain.value = true; writeError.value = '未收到与本次目标匹配的回执，请只读核对任务状态。'
    }
    stale.value = true
  } catch (error) {
    if (current !== generation) return
    // 通信失败不代表服务端没有执行，绝不把重新发起写操作当作恢复。
    const rejected = error instanceof ApiError && error.status >= 400 && error.status < 500
    uncertain.value = !rejected
    writeError.value = rejected ? errorMessage(error) : '请求回执未知，可能已受理。请更新执行状态，暂不重复提交。'
    if (error instanceof ApiError && [401, 403].includes(error.status)) permissionDenied.value = true
    stale.value = true
  } finally {
    clearTimeout(timer)
    if (current === generation) { writing.value = false; writeController = null }
  }
  if (current === generation) await refresh()
}
watch(selectedId, () => { impactConfirmed.value = false })
watch(() => props.coreId, () => {
  generation++; readController?.abort(); writeController?.abort()
  detail.value = null; selectedId.value = ''; level.value = ''; reading.value = false; writing.value = false
  readError.value = ''; writeError.value = ''; notice.value = ''; lastReadAt.value = null
  uncertain.value = false; permissionDenied.value = false; stale.value = true; impactConfirmed.value = false
  void refresh()
}, { immediate: true })
onUnmounted(() => { generation++; readController?.abort(); writeController?.abort() })
</script>

<template>
  <section class="core-control-panel" aria-label="核心配置控制" :aria-busy="reading || writing">
    <header><h3>核心配置</h3><RefreshControl :loading="reading" :disabled="writing" :error="readError" :last-read-at="lastReadAt" label="更新执行状态" @refresh="refresh" /></header>
    <p class="small muted">当前仅支持日志级别变更。完整配置、协议、Mux 和核心升级尚未开放。</p>
    <p v-if="reading && !detail" role="status">正在读取此核心的控制能力与任务…</p>
    <p v-if="readError" class="quality-note" role="alert">{{ readError }}</p>
    <p v-if="limitation" class="quality-note" role="status">{{ limitation }}</p>
    <p v-if="writeError" class="quality-note" role="alert">{{ writeError }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <template v-if="detail">
      <dl class="control-facts"><div><dt>当前修订</dt><dd>{{ detail.revision }}</dd></div><div><dt>配置版本</dt><dd>{{ detail.configuration_version || '未知' }}</dd></div></dl>
      <form class="candidate-form" @submit.prevent="submit('candidate')">
        <label>候选日志级别<select v-model="level" aria-label="候选日志级别" :disabled="!ready"><option v-for="option in detail.capabilities.log_levels" :key="option" :value="option">{{ option }}</option></select></label>
        <button type="submit" :disabled="!canCreate">{{ writing ? '正在提交…' : '生成候选' }}</button>
      </form>
      <p v-if="!detail.candidates.length" class="small muted">暂无配置候选。生成候选不会应用到当前核心。</p>
      <label v-else class="candidate-picker">查看候选<select v-model="selectedId" aria-label="查看核心候选" :disabled="writing"><option v-for="candidate in detail.candidates" :key="candidate.id" :value="candidate.id">{{ candidate.difference.after || '未知' }} · {{ candidateLabels[candidate.state] || '状态未知' }} · {{ formatDate(candidate.created_at) }}</option></select></label>
      <section v-if="selected" class="candidate-preview" aria-label="候选差异与影响">
        <h4>候选差异与影响</h4>
        <p><strong>日志级别：</strong>{{ selected.difference.before || '未明确设置' }} → {{ selected.difference.after || '未知' }}</p>
        <p>状态：{{ candidateLabels[selected.state] || '状态未知' }}；基于修订 {{ selected.base_revision }}</p>
        <p v-if="selected.base_revision !== detail.revision && selected.id !== detail.current_applied_candidate_id" class="quality-note">候选版本已过期，请基于当前修订重新生成。</p>
        <p>{{ selected.impact.message }}</p>
        <p class="small muted">检查通过不代表连接、UDP、DNS 或计量完整性已验收。</p>
        <p v-if="selected.checked_at" class="small muted">检查完成：{{ formatDate(selected.checked_at) }}。执行前仍需重新核对来源、身份与配置版本。</p>
        <details><summary>候选校验摘要</summary><dl class="control-facts"><div><dt>原配置摘要</dt><dd>{{ selected.base_sha256 }}</dd></div><div><dt>候选摘要</dt><dd>{{ selected.candidate_sha256 }}</dd></div></dl></details>
        <label v-if="canApply || canRollback" class="impact-confirm"><input v-model="impactConfirmed" type="checkbox" :disabled="writing" />我已核对本候选差异及连接中断影响</label>
        <div class="control-actions"><button :disabled="!canCheck" @click="submit('check')">检查候选</button><button :disabled="!canApply || !impactConfirmed" @click="submit('apply')">应用候选</button><button v-if="selected.id === detail.current_applied_candidate_id" :disabled="!canRollback || !impactConfirmed" @click="submit('rollback')">回滚当前候选</button></div>
      </section>
      <section class="core-jobs" aria-label="核心执行任务"><h4>执行任务</h4><p v-if="!detail.jobs.length" class="small muted">暂无执行记录。</p>
        <article v-for="job in detail.jobs" :key="job.id" class="core-job"><strong>{{ actionLabels[job.action] || '核心操作' }} · {{ jobLabels[job.status] || '结果未知' }}</strong>
          <p class="small muted">提交：{{ formatDate(job.created_at) }}<template v-if="job.finished_at"> · 回执：{{ formatDate(job.finished_at) }}</template></p>
          <p v-if="job.status === 'unknown'" class="quality-note">结果未知，不代表执行失败。请先只读核对，不能重复重启或自动回滚。</p>
          <template v-if="job.error_code"><p>{{ errorLabels[job.error_code] || '执行未取得可确认结果，请核对任务回执后处理。' }}</p><details><summary>错误详情</summary><p>原因代码：{{ job.error_code }}</p></details></template>
          <dl v-if="job.result.core_version || job.result.config_sha256" class="control-facts"><div v-if="job.result.core_version"><dt>回执核心版本</dt><dd>{{ job.result.core_version }}</dd></div><div v-if="job.result.config_sha256"><dt>回执配置摘要</dt><dd>{{ job.result.config_sha256 }}</dd></div></dl>
        </article>
      </section>
    </template>
  </section>
</template>

<style scoped>
.core-control-panel{margin-top:24px;padding-top:20px;border-top:1px solid #dfe6f1;min-width:0;overflow-wrap:anywhere}.core-control-panel header{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.core-control-panel h3,.core-control-panel h4{margin:0 0 12px}.control-facts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.control-facts dt{font-size:12px;color:#63748f}.control-facts dd{margin:5px 0 0;overflow-wrap:anywhere}.candidate-form,.control-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:end}.candidate-form label,.candidate-picker{display:grid;gap:7px;min-width:0}.candidate-picker{margin-top:18px}.core-control-panel select{width:100%;max-width:100%;min-width:0;font:inherit;padding:10px;border:1px solid #b5c2d8;border-radius:8px;background:white;color:inherit}.core-control-panel button{min-height:42px;max-width:100%;font:inherit;padding:9px 14px;border:1px solid #b5c2d8;border-radius:8px;background:white;color:inherit;cursor:pointer}.core-control-panel button:disabled,.core-control-panel select:disabled{opacity:.55;cursor:not-allowed}.candidate-preview,.core-job{margin-top:16px;padding:16px;border:1px solid #dfe6f1;border-radius:10px;background:#fff;min-width:0}.impact-confirm{display:flex;align-items:flex-start;gap:8px;margin:16px 0}.impact-confirm input{flex-shrink:0;margin-top:5px}.core-jobs{margin-top:22px}.core-job p{margin:9px 0}.control-actions{margin-top:14px}.core-control-panel summary{cursor:pointer}.core-control-panel :focus-visible{outline:3px solid #517ee1;outline-offset:3px}@media(max-width:600px){.core-control-panel header{display:block}.control-facts{grid-template-columns:1fr}.candidate-form{align-items:stretch}.candidate-form label{flex:1 1 100%}.candidate-form button{width:100%}.control-actions button{flex:1 1 100%}.candidate-preview,.core-job{padding:12px}}
</style>
