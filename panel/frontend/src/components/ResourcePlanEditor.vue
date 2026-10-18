<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ApiError, errorMessage, request } from '../api'
import type { ResourcePlanRead, ResourcePlanPreview, ResourcePlanConfirm, ResourcePlanChanges, ResourcePlanOperation } from '../resourcePlanTypes'
import RefreshControl from './RefreshControl.vue'

const props = defineProps<{ bindingKey: string; enabled: boolean }>()
const emit = defineEmits<{ saved: [result: ResourcePlanConfirm] }>()
const current = ref<ResourcePlanRead | null>(null), latest = ref<ResourcePlanRead | null>(null)
const preview = ref<ResourcePlanPreview | null>(null), receipt = ref<ResourcePlanConfirm['receipt'] | null>(null)
const operation = ref<ResourcePlanOperation>('quota'), quotaMode = ref<'set' | 'add'>('set')
const quotaGB = ref(''), additionGB = ref(''), expiresOn = ref(''), cycleDays = ref(''), nextReset = ref('')
const busy = ref(false), error = ref(''), conflict = ref(false), denied = ref(false)
const readBusy = ref(false), readError = ref(''), lastReadAt = ref<string | null>(null)
type Confirmation = { expected_revision: number; source_fingerprint: string; operation: ResourcePlanOperation; changes: ResourcePlanChanges; preview_token: string; idempotency_key: string }
const pending = ref<Confirmation | null>(null)
let generation = 0
const endpoint = () => '/admin/resource-plans/' + encodeURIComponent(props.bindingKey)
const allowed = computed(() => current.value?.allowed_operations.includes(operation.value) === true)
const editable = computed(() => props.enabled && !denied.value && allowed.value && !busy.value && !pending.value && !conflict.value)
const labels: Record<ResourcePlanOperation, string> = { quota: '额度与追加额', expiry: '到期与续费', cycle: '重置周期' }

function bytesToGB(value: string | null): string {
  if (value === null || !/^\d+$/.test(value)) return ''
  const bytes = BigInt(value), fraction = (bytes % 1_000_000_000n).toString().padStart(9, '0').replace(/0+$/, '')
  return (bytes / 1_000_000_000n).toString() + (fraction ? '.' + fraction : '')
}
function quotaLabel(value: string | null) { return value === null ? '来源未提供' : bytesToGB(value) + ' GB' }
function timeLabel(value: string | null) {
  if (!value) return '尚无记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间暂不可确认' : new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}
function seed(data: ResourcePlanRead) {
  quotaGB.value = bytesToGB(data.plan.quota_bytes); additionGB.value = ''; expiresOn.value = data.plan.expires_on || ''
  cycleDays.value = data.plan.cycle.kind === 'fixed_days' ? String(data.plan.cycle.days) : ''
  // 锚点只用于展示既有计划；新重置时间由管理员独立确认。
  nextReset.value = ''
  if (!data.allowed_operations.includes(operation.value)) operation.value = data.allowed_operations[0] || 'expiry'
  if (data.consumption.status === 'rejected' && data.allowed_operations.includes('cycle')) operation.value = 'cycle'
}
function acceptError(cause: unknown) {
  error.value = errorMessage(cause)
  if (cause instanceof ApiError && (cause.status === 401 || cause.status === 403)) {
    denied.value = true; current.value = null; latest.value = null; preview.value = null; receipt.value = null
  }
  if (cause instanceof ApiError && cause.status === 409) conflict.value = true
}
async function load(forConflict = false) {
  if (busy.value || !props.bindingKey || denied.value) return
  const ticket = generation; busy.value = true; readBusy.value = true; error.value = ''; readError.value = ''
  try {
    const data = await request<ResourcePlanRead>(endpoint())
    if (ticket !== generation) return
    lastReadAt.value = new Date().toISOString()
    if (forConflict) latest.value = data
    else {
      const initial = current.value === null
      if (!pending.value && current.value && (current.value.revision !== data.revision || current.value.source_fingerprint !== data.source_fingerprint)) preview.value = null
      current.value = data; if (initial) seed(data)
    }
  } catch (cause) { if (ticket === generation) { readError.value = errorMessage(cause); acceptError(cause) } }
  finally { if (ticket === generation) { busy.value = false; readBusy.value = false } }
}
function adoptLatest() {
  if (!latest.value || busy.value || !props.enabled) return
  // 只有明确核对冲突版本后才结束旧请求；保留所有表单输入，不自动提交。
  current.value = latest.value; latest.value = null; conflict.value = false; pending.value = null; preview.value = null; error.value = ''
}
function changeOperation(next: ResourcePlanOperation) {
  if (busy.value || pending.value || conflict.value) return
  operation.value = next; preview.value = null; error.value = ''
}
function recoverCycle() {
  if (!props.enabled || denied.value || !current.value?.allowed_operations.includes('cycle') || busy.value || pending.value || conflict.value) return
  changeOperation('cycle')
  // 已输入的未来时间保留；过期时间必须由管理员重新选择，不能自动推迟。
  if (Date.parse(nextReset.value + ':00+08:00') <= Date.now()) nextReset.value = ''
}
function amount(value: string, positive = false): string {
  const normalized = value.trim()
  if (!/^\d+(?:\.\d{1,9})?$/.test(normalized)) throw new Error('请输入非负 GB 数值，最多保留 9 位小数。')
  const [whole, fraction = ''] = normalized.split('.')
  const bytes = BigInt(whole!) * 1_000_000_000n + BigInt(fraction.padEnd(9, '0'))
  if (positive && bytes === 0n) throw new Error('追加额度必须大于 0。')
  return bytes.toString()
}
function changes(): ResourcePlanChanges {
  if (operation.value === 'quota') return quotaMode.value === 'add' ? { add_bytes: amount(additionGB.value, true) } : { quota_bytes: amount(quotaGB.value) }
  if (operation.value === 'expiry') {
    if (expiresOn.value && !/^\d{4}-\d{2}-\d{2}$/.test(expiresOn.value)) throw new Error('请选择完整的采购到期日期。')
    return { expires_on: expiresOn.value || null }
  }
  if (!/^\d+$/.test(cycleDays.value) || Number(cycleDays.value) < 1 || Number(cycleDays.value) > 366) throw new Error('周期天数必须是 1 至 366 的整数。')
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(nextReset.value) || !Number.isFinite(Date.parse(nextReset.value + ':00+08:00'))) throw new Error('请选择下次重置时间（上海）。')
  return { days: Number(cycleDays.value), next_reset_at: nextReset.value + ':00+08:00' }
}
async function makePreview() {
  if (!editable.value || !current.value) return
  let body: { expected_revision: number; source_fingerprint: string; operation: ResourcePlanOperation; changes: ResourcePlanChanges }
  try { body = { expected_revision: current.value.revision, source_fingerprint: current.value.source_fingerprint, operation: operation.value, changes: changes() } }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '输入无效。'; return }
  const ticket = generation; busy.value = true; error.value = ''; preview.value = null
  try {
    const data = await request<ResourcePlanPreview>(endpoint() + '/preview', 'POST', body)
    if (ticket === generation) preview.value = data
  } catch (cause) { if (ticket === generation) acceptError(cause) }
  finally { if (ticket === generation) busy.value = false }
}
async function confirm() {
  if (busy.value || !props.enabled || denied.value || conflict.value || !current.value || !allowed.value || (!preview.value && !pending.value)) return
  if (!pending.value) {
    const data = preview.value!
    pending.value = { expected_revision: data.revision, source_fingerprint: data.source_fingerprint, operation: data.operation, changes: data.changes, preview_token: data.preview_token, idempotency_key: crypto.randomUUID() }
  }
  const ticket = generation; busy.value = true; error.value = ''
  try {
    const data = await request<ResourcePlanConfirm>(endpoint() + '/confirm', 'POST', pending.value)
    if (ticket !== generation) return
    if (!data.receipt?.id || !Number.isSafeInteger(data.receipt.revision) || !['pending', 'consumed', 'rejected'].includes(data.receipt.status) || data.meter_id !== current.value?.meter_id) {
      throw new ApiError(200, 'INVALID_RESPONSE', '保存响应缺少有效回执，请保留原请求并重新核对。')
    }
    current.value = data; receipt.value = data.receipt; pending.value = null; preview.value = null; emit('saved', data)
  } catch (cause) {
    if (ticket !== generation) return
    acceptError(cause)
    // 通信结果不确定或冲突时保留原请求，不能换 key 猜测是否已经保存。
    if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500 && cause.status !== 409 && cause.code !== 'INVALID_RESPONSE') { pending.value = null; preview.value = null }
  } finally { if (ticket === generation) busy.value = false }
}
watch([quotaGB, additionGB, quotaMode, expiresOn, cycleDays, nextReset], () => { if (!pending.value) preview.value = null }, { flush: 'sync' })
watch(() => props.bindingKey, () => {
  generation++; busy.value = false; current.value = null; latest.value = null; preview.value = null; pending.value = null; receipt.value = null
  conflict.value = false; denied.value = false; error.value = ''; readBusy.value = false; readError.value = ''; lastReadAt.value = null; void load()
}, { immediate: true, flush: 'sync' })
watch(() => props.enabled, enabled => { if (!enabled) { generation++; busy.value = false; readBusy.value = false; preview.value = null } })
onUnmounted(() => { generation++ })
defineExpose({ load })
</script>

<template>
  <section class="resource-plan-editor" aria-label="采购计划设置" :aria-busy="busy">
    <header><div><h3>采购计划设置</h3><p>仅维护此资源的采购额度、到期日期和重置计划。</p></div><RefreshControl :loading="readBusy" :disabled="busy || denied || !!pending" :error="readError" :last-read-at="lastReadAt" label="读取计划状态" @refresh="load()" /></header>
    <p v-if="!enabled" class="muted">此环境未开放采购计划修改。</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="!current && busy" role="status">正在读取采购计划…</p>
    <template v-if="current">
      <p class="plan-source">{{ current.plan.label }} · {{ current.plan.source === 'bwh' ? '供应商官方来源' : '已登记采购来源' }} · 计划版本 {{ current.revision }}</p>
      <h4>当前有效计划</h4>
      <dl v-if="current.effective_plan" class="current-plan" aria-label="当前有效采购计划"><div><dt>采购额度</dt><dd>{{ quotaLabel(current.effective_plan.quota_bytes) }}</dd></div><div><dt>采购到期</dt><dd>{{ current.effective_plan.expires_on || '未登记' }}</dd></div><div><dt>重置计划</dt><dd>{{ current.effective_plan.cycle.kind === 'provider' ? '供应商官方重置（只读）' : `每 ${current.effective_plan.cycle.days} 天` }}</dd></div></dl>
      <p v-else class="muted">当前有效计划暂不可读取。</p>
      <div class="consumption" role="status">
        <p v-if="receipt">采购计划已保存，回执版本 {{ receipt.revision }}{{ receipt.replayed ? '（原请求回执）' : '' }}。{{ receipt.status === 'rejected' ? '该版本未能生效。' : '' }}</p>
        <p v-if="current.consumption.status === 'consumed' && current.consumption.revision === current.revision">版本 {{ current.consumption.revision }} 已生效，更新于 {{ timeLabel(current.consumption.consumed_at) }}。</p>
        <template v-else-if="current.consumption.status === 'rejected'"><p class="error">{{ current.consumption.error_code === 'cycle_reset_expired' ? '重置时间已过，请重新选择。' : '当前目标计划未能生效，请核对后重新预览。' }}</p><p>原有效计划与统计保持不变，请在下方重选目标周期。</p><button v-if="current.allowed_operations.includes('cycle')" type="button" :disabled="busy || !enabled || denied || !!pending || conflict" @click="recoverCycle">重新选择重置时间</button></template>
        <p v-else-if="current.consumption.status === 'pending'">等待生效：设置已保存，将在统计更新后显示结果。</p>
        <p v-else>尚未修改，当前使用原采购设置。</p>
      </div>
      <div class="operation-tabs" role="group" aria-label="采购修改项目"><button v-for="(label, key) in labels" :key="key" type="button" :aria-pressed="operation === key" :disabled="busy || !!pending || conflict" @click="changeOperation(key)">{{ label }}</button></div>
      <p v-if="!allowed" class="readonly-note">此项目由供应商官方来源管理，只读展示；不能用人工计划覆盖官方额度或重置时间。</p>
      <form v-if="allowed" @submit.prevent="makePreview">
        <fieldset :disabled="!editable">
          <legend>{{ labels[operation] }}</legend>
          <template v-if="operation === 'quota'">
            <label>额度操作<select v-model="quotaMode" aria-label="额度操作"><option value="set">设置采购总额度</option><option value="add">追加采购额度</option></select></label>
            <label v-if="quotaMode === 'set'">采购额度（GB）<input v-model="quotaGB" inputmode="decimal" aria-label="采购额度（GB）" autocomplete="off"></label>
            <label v-else>追加额度（GB）<input v-model="additionGB" inputmode="decimal" aria-label="追加额度（GB）" autocomplete="off"></label>
            <p class="hint">1 GB = 1,000,000,000 字节；修改额度不会清空已用量。</p>
          </template>
          <template v-else-if="operation === 'expiry'"><label>采购到期日期<input v-model="expiresOn" type="date" aria-label="采购到期日期"></label><p class="hint">留空表示清除登记日期。续费日期独立保存，不改变额度、重置计划或既有用量。</p></template>
          <template v-else><label>周期天数<input v-model="cycleDays" type="number" min="1" max="366" step="1" aria-label="周期天数"></label><label>下次重置时间（上海）<input v-model="nextReset" type="datetime-local" aria-label="下次重置时间（上海）"></label><p class="hint">只影响未来重置，既有账期和历史用量保持不变。</p></template>
        </fieldset>
        <button type="submit" class="primary" :disabled="!editable">预览变更</button>
      </form>
      <section v-if="preview" class="preview" aria-label="采购变更预览"><h4>{{ labels[preview.operation] }}：保存前核对</h4>
        <div class="diff-grid"><div><h5>已保存的目标计划</h5><p v-if="preview.operation === 'quota'">{{ quotaLabel(preview.before.quota_bytes) }}</p><p v-else-if="preview.operation === 'expiry'">{{ preview.before.expires_on || '未登记' }}</p><p v-else>{{ preview.before.cycle.kind === 'fixed_days' ? `每 ${preview.before.cycle.days} 天；原锚点 ${timeLabel(preview.before.cycle.anchor)}` : '供应商官方重置' }}</p></div>
        <div><h5>待保存的目标计划</h5><p v-if="preview.operation === 'quota'">{{ quotaLabel(preview.after.quota_bytes) }}</p><p v-else-if="preview.operation === 'expiry'">{{ preview.after.expires_on || '未登记' }}</p><p v-else>{{ preview.after.cycle.kind === 'fixed_days' ? `每 ${preview.after.cycle.days} 天；下次重置 ${timeLabel(preview.after.cycle.anchor)}` : '供应商官方重置' }}</p></div></div>
        <ul><li v-for="effect in preview.effects" :key="effect">{{ effect }}</li></ul>
        <button type="button" class="primary" :disabled="busy || !enabled || conflict || !!pending" @click="confirm">确认保存采购计划</button>
      </section>
      <div v-if="pending && !conflict" class="pending"><p>上次保存尚未取得确定回执，输入与原请求已保留。</p><button type="button" :disabled="busy || !enabled || denied" @click="confirm">重试原保存请求</button></div>
      <div v-if="conflict" class="conflict"><p>计划版本或来源已变化，原输入已保留。请核对最新计划后重新预览。</p><button type="button" :disabled="busy || denied" @click="load(true)">读取当前版本，保留输入</button><div v-if="latest"><p>当前版本 {{ latest.revision }}：额度 {{ quotaLabel(latest.plan.quota_bytes) }}；到期 {{ latest.plan.expires_on || '未登记' }}；周期 {{ latest.plan.cycle.kind === 'provider' ? '供应商官方重置' : `每 ${latest.plan.cycle.days} 天` }}。</p><button type="button" :disabled="busy || !enabled" @click="adoptLatest">采用当前版本并重新预览</button></div></div>
    </template>
  </section>
</template>

<style scoped>
.resource-plan-editor{min-width:0;margin-top:22px;padding:20px;border:1px solid var(--border,#dfe6ed);border-radius:14px;background:#fff;color:var(--text,#213652)}
.operation-tabs button{padding-inline:7px}
header{display:flex;justify-content:space-between;gap:14px;align-items:flex-start}header>div{min-width:0}h3{margin:0;font-size:18px}h4{margin:0 0 12px}h5{margin:0;font-size:13px;color:#687887}p,li{font-size:13px;line-height:1.65;overflow-wrap:anywhere}header p{margin:6px 0;color:#687887}.muted,.hint{color:#687887}.plan-source{font-size:12px}.current-plan{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin:18px 0}.current-plan dt{font-size:12px;color:#687887}.current-plan dd{margin:5px 0 0;font-size:14px;overflow-wrap:anywhere}.consumption{padding:4px 12px;background:#f3f6fb;border-radius:8px}.operation-tabs{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0 14px}button{box-sizing:border-box;max-width:100%;border:1px solid #ccd7e5;background:#fff;border-radius:8px;padding:9px 13px;font:inherit;font-size:13px;white-space:normal;overflow-wrap:anywhere;cursor:pointer;transition:background .15s,border-color .15s}button:hover:not(:disabled){background:#edf2ff;border-color:#5874df}button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #b7c6ff;outline-offset:2px}button[aria-pressed=true],button.primary{background:#365ce8;color:white;border-color:#365ce8}button.primary:hover:not(:disabled){background:#2648c6}button:disabled{opacity:.55;cursor:not-allowed}fieldset{min-width:0;margin:0 0 14px;padding:0;border:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}legend{font-size:14px;font-weight:600;margin-bottom:14px}label{display:flex;flex-direction:column;gap:7px;min-width:0;font-size:13px}input,select{box-sizing:border-box;width:100%;min-width:0;max-width:100%;border:1px solid #cbd5e3;border-radius:7px;padding:9px;font:inherit;font-size:14px;background:#fff;color:#213652}fieldset .hint{grid-column:1/-1;margin:0}.preview,.conflict,.pending{margin-top:16px;padding:16px;border-radius:10px;background:#f4f7ff}.diff-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.diff-grid p{font-size:15px}.preview ul{padding-left:18px}.error{color:#af2435}.readonly-note,.conflict,.pending{color:#825c16;background:#fff8e9}.readonly-note{padding:12px;border-radius:8px}@media(max-width:600px){.resource-plan-editor{padding:14px}header{flex-direction:column}.current-plan{grid-template-columns:1fr}.operation-tabs button{flex:1 1 100px}fieldset,.diff-grid{grid-template-columns:1fr}button{line-height:1.5}}
</style>
