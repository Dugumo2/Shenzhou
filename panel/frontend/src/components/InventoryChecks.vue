<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { formatDate } from '../display'
import { ApiError, errorMessage, request } from '../api'
import type { InventoryCheck, InventoryCheckAction, InventoryCheckJob } from '../inventoryTypes'
import { effectiveFreshness, useInventoryClock } from '../inventoryStatus'
const props = withDefaults(defineProps<{ checks: { items: InventoryCheck[]; truncated: boolean }; actions?: InventoryCheckAction[] }>(), { actions: () => [] })
const emit = defineEmits<{ completed: [] }>()
const now = useInventoryClock()
const job = ref<InventoryCheckJob | null>(null)
const busy = ref(false)
const message = ref('')
const permissionDenied = ref(false)
const requestDeadlineMs = 15_000
const pending = ref<{target_kind:string;target_id:string;target_version:string;action:string;idempotency_key:string} | null>(null)
let epoch = 0
let timer: ReturnType<typeof setTimeout> | undefined
let controller: AbortController | undefined
const signature = computed(() => props.actions.map(a => [a.target_kind,a.target_id,a.target_version,a.action].join(':')).join('|'))
const active = computed(() => job.value && job.value.state !== 'completed')
const jobStates: Record<string,string> = {queued:'等待后台执行',running:'正在检查',completed:'检查已结束'}
const failures: Record<string,string> = {permission_denied:'管理员权限已失效',target_version_changed:'目标版本已变化，旧任务已停止',target_unavailable:'目标关系无效或超出检查范围，请先核对资源资料',worker_deadline_exceeded:'后台未在期限内完成，请读取状态或稍后重试',action_not_allowed:'此动作已从批准目录移除',action_policy_changed:'固定检查范围在执行期间发生变化，结果未采纳',policy_unavailable:'检查目录暂不可用',invalid_policy:'检查目录不符合要求'}
function reset() {
  epoch++; clearTimeout(timer); controller?.abort(); busy.value=false; job.value=null; pending.value=null; message.value=''; permissionDenied.value=false
}
watch(signature, reset, { flush: 'sync' })
onBeforeUnmount(reset)
async function boundedRequest(path: string, method: string, body: unknown, requestController: AbortController) {
  // 独立结束等待，不能依赖底层请求一定响应 abort；迟到结果不会重新进入组件。
  let deadline: ReturnType<typeof setTimeout> | undefined
  let cancel: () => void = () => {}
  try {
    return await new Promise<InventoryCheckJob>((resolve, reject) => {
      cancel = () => reject(new ApiError(0, 'REQUEST_ABORTED', '本次请求已取消。'))
      requestController.signal.addEventListener('abort', cancel, { once: true })
      deadline = setTimeout(() => {
        reject(new ApiError(0, 'CHECK_REQUEST_TIMEOUT', method === 'POST'
          ? '提交等待已超过15秒，结果未知。请求标识已保留；可手动重试同一检查以确认原任务。'
          : '读取等待已超过15秒，当前任务已保留，结果未知；可手动重新读取任务状态。'))
        requestController.abort()
      }, requestDeadlineMs)
      request<InventoryCheckJob>(path, method, body, requestController.signal).then(resolve, reject)
    })
  } finally {
    clearTimeout(deadline)
    requestController.signal.removeEventListener('abort', cancel)
  }
}
function failed(error: unknown) {
  message.value=errorMessage(error)
  if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
    // 撤权后不沿用旧目录恢复动作，也不继续读取旧任务。
    permissionDenied.value=true; pending.value=null; job.value=null; clearTimeout(timer)
  }
}
function accept(value: InventoryCheckJob) {
  job.value=value
  if (value.state === 'completed') {
    pending.value=null
    message.value=value.failure_code ? (failures[value.failure_code] || '检查未完成，请查看失败代码。') : '检查回执已保存；结论仅限本次检测层级。'
    emit('completed')
    return
  }
  if (value.lease_until && Date.parse(value.lease_until) <= Date.now()) {
    message.value='等待已超过后台期限；当前结果未知，可重新读取任务状态。'
    return
  }
  message.value=value.state === 'queued' ? '检查已提交，等待受限后台执行。' : '后台正在执行固定检查。'
  timer=setTimeout(() => { void readJob() }, 1500)
}
async function readJob() {
  if (!job.value || busy.value || permissionDenied.value) return
  const own=epoch, id=job.value.id
  busy.value=true; clearTimeout(timer); controller=new AbortController()
  try {
    const result=await boundedRequest(`/admin/resource-checks/${encodeURIComponent(id)}`, 'GET', undefined, controller)
    if(own===epoch) accept(result)
  } catch(error) { if(own===epoch) failed(error) }
  finally { if(own===epoch) busy.value=false }
}
async function start(action: InventoryCheckAction) {
  if (busy.value || active.value || permissionDenied.value || !props.actions.some(a => a.target_id===action.target_id && a.target_kind===action.target_kind && a.target_version===action.target_version && a.action===action.action)) return
  if (pending.value && (pending.value.target_id!==action.target_id || pending.value.target_kind!==action.target_kind || pending.value.target_version!==action.target_version || pending.value.action!==action.action)) return
  pending.value ||= {target_kind:action.target_kind,target_id:action.target_id,target_version:action.target_version,action:action.action,idempotency_key:crypto.randomUUID()}
  const own=epoch; busy.value=true; message.value='正在提交固定检查…'; controller=new AbortController()
  try {
    const value=await boundedRequest('/admin/resource-checks','POST',pending.value,controller)
    if(own===epoch) accept(value)
  } catch(error) {
    if(own===epoch) {
      failed(error)
      // 网络中断保留同一请求标识，重试不会再执行一次。
      if(error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 409) pending.value=null
    }
  } finally { if(own===epoch) busy.value=false }
}
const states: Record<string,string> = { fresh:'有效期内', stale:'已过期', target_changed:'目标已变更', unknown:'未知' }
const results: Record<string,string> = { pass:'本层通过', fail:'失败', timeout:'超时', unknown:'未知 / NOT TESTED' }
const kinds: Record<string,string> = { service_status:'服务状态', core_version:'核心版本', tcp:'TCP连接', udp:'UDP', tls:'TLS握手', dns:'DNS解析', egress:'出口', whole_path:'整链' }
const targets: Record<string,string> = { server:'服务器', core:'核心', ingress:'入口', egress:'出口', line:'线路' }
</script>
<template>
  <section class="inventory-checks"><h2>检测记录</h2><p class="small muted">按具体目标分别记录；单端点通过不代表整条线路可用。刷新页面不会发起检测。</p>
    <div v-if="actions.length" class="check-actions">
      <h3>发起固定检查</h3>
      <div v-for="action in actions" :key="`${action.target_kind}:${action.target_id}:${action.action}`" class="check-action">
        <el-button :disabled="busy || permissionDenied || !!active || (!!pending && (pending.target_id !== action.target_id || pending.target_kind !== action.target_kind || pending.action !== action.action))" @click="start(action)">{{ pending && !job ? '重试：' : '' }}{{ action.label }}</el-button>
        <p class="small muted">{{ action.scope }} · 最长 {{ action.timeout_seconds }} 秒 · 每目标间隔 {{ action.min_interval_seconds }} 秒</p>
      </div>
    </div>
    <p v-else class="small muted">此资源暂无已批准的固定检查动作。</p>
    <p class="small muted">本批固定动作不测试认证、DNS、UDP、出口与整链，这些阶段均为 NOT TESTED。</p>
    <p v-if="actions.length" class="small muted">提交和读取最多等待15秒；等待超时不代表后台检查失败或通过，不会自动重发检查。</p>
    <div v-if="job" class="job-status">
      <strong>{{ jobStates[job.state] }}</strong><span> · {{ kinds[job.action] || job.action }} · {{ targets[job.target_kind] || job.target_kind }} #{{ job.target_id.slice(0,8) }}</span>
      <p class="small">任务 {{ job.id }} · 目标版本 {{ job.target_version.slice(0,12) }}</p>
      <p class="small muted">提交 {{ formatDate(job.created_at) }} · 开始 {{ formatDate(job.started_at) }} · 完成 {{ formatDate(job.finished_at) }}</p>
      <p v-if="job.failure_code" class="small">未完成代码：{{ job.failure_code }}</p>
      <p v-if="job.result">{{ results[job.result.result] }} · {{ job.result.latency_ms ?? '未知' }} ms <span v-if="job.result.error_stage">· 失败阶段 {{ job.result.error_stage }} / {{ job.result.error_code }}</span></p>
      <el-button v-if="active" :disabled="busy" @click="readJob">读取任务状态</el-button>
    </div>
    <p v-if="message" role="status" aria-live="polite">{{ message }}</p>
    <p v-if="!checks.items.length" class="muted">暂无可信检测回执。</p>
    <article v-for="check in checks.items" :key="check.id">
      <div><strong>{{ kinds[check.check_kind] || check.check_kind }}</strong> · {{ targets[check.target_kind] || check.target_kind }} #{{ check.target_id.slice(0,8) }} <el-tag type="info">{{ states[effectiveFreshness(check.freshness, check.expires_at, now)] }}</el-tag></div>
      <p>本次结果：{{ results[check.result] }} <span v-if="check.latency_ms !== null">· {{ check.latency_ms }} ms</span></p>
      <p class="small muted">目标版本 {{ check.target_version ? check.target_version.slice(0,12) : '历史回执未提供' }}</p>
      <p class="small muted">来源 {{ check.source }} · 检测 {{ formatDate(check.observed_at) }} · 有效至 {{ formatDate(check.expires_at) }}</p>
      <p v-if="check.error_stage || check.error_code" class="small">失败阶段 {{ check.error_stage || '未提供' }} · {{ check.error_code || '未提供' }}</p>
    </article><p v-if="checks.truncated" class="small muted">仅显示最新100条回执。</p>
  </section>
</template>
<style scoped>.inventory-checks{margin:24px 0}.inventory-checks article{padding:14px 0;border-bottom:1px solid #e7ecf4}.inventory-checks h2{font-size:18px}.inventory-checks h3{font-size:15px}.inventory-checks p{overflow-wrap:anywhere}.check-actions,.job-status{padding:14px;border:1px solid #dce4ee;border-radius:12px;margin:12px 0}.check-action{margin:10px 0}.check-action .el-button{max-width:100%;height:auto;min-height:32px;white-space:normal;text-align:left}.check-action p{margin:6px 0}</style>
