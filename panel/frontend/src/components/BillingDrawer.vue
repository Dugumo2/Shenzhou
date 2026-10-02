<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { request, errorMessage, ApiError } from '../api'
import { auth } from '../auth'
import { boundedRequest, getPending, pendingKey, rememberPending, submitPending } from '../billingPending'
import { formatDate, formatGB, previewMatches, shanghaiInput, shiftLabel } from '../display'
import type { Billing, BillingPreview, Service } from '../types'
const props = defineProps<{ service: Service | null }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const billing = ref<Billing | null>(null), preview = ref<BillingPreview | null>(null)
const input = ref(''), previewInput = ref(''), busy = ref(false), saving = ref(false), error = ref(''), success = ref('')
const planReady = ref(false)
const identityKey = computed(() => props.service && auth.session?.authenticated && auth.session.user?.is_staff
  ? pendingKey(auth.session.user.username, props.service.id) : '')
const requestBody = computed(() => identityKey.value ? getPending(identityKey.value) : null)
function drawerRequest<T>(url: string, method = 'GET', body?: unknown): Promise<T> {
  return boundedRequest(request<T>(url, method, body))
}
const now = ref(Date.now())
const timer = setInterval(() => { now.value = Date.now() }, 1000)
onUnmounted(() => { generation++; clearInterval(timer) })
let generation = 0
// 已发出而结果未知的请求保留同一幂等键，可安全重试。
const canSave = computed(() => (!!requestBody.value || (planReady.value && billing.value?.can_modify && !!preview.value && previewMatches(input.value, previewInput.value, preview.value.preview_expires_at, now.value))) && !busy.value && !saving.value)
const path = computed(() => '/admin/services/' + encodeURIComponent(props.service?.id || '') + '/billing')
async function refreshPlan() {
  if (requestBody.value) { error.value = '请先重试确认上次保存结果，再修改或刷新计划。'; return false }
  const current = generation
  busy.value = true; planReady.value = false; error.value = ''; success.value = ''; preview.value = null
  try {
    const data = await drawerRequest<Billing>(path.value)
    if (current === generation) { billing.value = data; planReady.value = true; return true }
    return false
  }
  catch (e) { if (current === generation) error.value = errorMessage(e); return false }
  finally { if (current === generation) busy.value = false }
}
async function showFailure(failure: unknown, current: number) {
  if (current !== generation) return
  const message = errorMessage(failure)
  error.value = message
  if (!(failure instanceof ApiError) || failure.status !== 409 || requestBody.value) return
  // 预览或保存冲突都重新读取计划；保留输入，刷新失败也不得冒充已刷新。
  const preservedInput = input.value
  const refreshed = await refreshPlan()
  if (current !== generation) return
  const refreshError = error.value
  input.value = preservedInput
  error.value = message + (refreshed ? ' 当前计划已刷新，输入已保留，请重新预览影响。' : ' 输入已保留，当前计划刷新失败：' + refreshError + ' 请先刷新当前计划再重新预览。')
}
watch(input, () => { if (requestBody.value) return; preview.value = null; previewInput.value = ''; success.value = '' }, { flush: 'sync' })
watch(identityKey, async key => {
  const current = ++generation
  billing.value = null; preview.value = null; input.value = ''; error.value = ''; success.value = ''; saving.value = false; busy.value = false; planReady.value = false
  if (!key) return
  if (requestBody.value) input.value = requestBody.value.next_reset_at
  busy.value = true
  try { const data = await drawerRequest<Billing>(path.value); if (current === generation) { billing.value = data; planReady.value = true; if (!requestBody.value) input.value = shanghaiInput(data.plan?.next_reset_at || data.current_cycle?.ends_at || null) } }
  catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}, { immediate: true, flush: 'sync' })
async function createPreview() {
  if (requestBody.value) { error.value = '请先重试确认上次保存结果。'; return }
  if (!planReady.value || !billing.value?.can_modify || !input.value || busy.value || saving.value) { error.value = '请先读取可修改的当前计划，并选择下一次流量重置时间。'; return }
  busy.value = true; error.value = ''; success.value = ''; preview.value = null
  const chosen = input.value, current = generation
  try {
    const data = await drawerRequest<BillingPreview>(path.value + '/preview', 'POST', { next_reset_at: chosen, expected_billing_revision: billing.value.billing_revision })
    if (current === generation && input.value === chosen) { billing.value = data; preview.value = data; previewInput.value = chosen }
  } catch (e) { await showFailure(e, current) }
  finally { if (current === generation) busy.value = false }
}
async function save() {
  if (!canSave.value || !props.service) { error.value = '请重新预览影响，再确认修改。'; return }
  saving.value = true; error.value = ''
  const current = generation, key = identityKey.value, savePath = path.value
  if (!key) { saving.value = false; return }
  let body = requestBody.value
  if (!body) {
    if (!preview.value || !billing.value) { saving.value = false; return }
    body = rememberPending(key, { next_reset_at: previewInput.value, expected_billing_revision: billing.value.billing_revision, preview_token: preview.value.preview_token, idempotency_key: crypto.randomUUID() })
  }
  try {
    const data = await submitPending(key, body, () => drawerRequest<{ message: string; billing: Billing }>(savePath, 'PATCH', body))
    if (current === generation) { billing.value = data.billing; planReady.value = true; input.value = shanghaiInput(data.billing.plan?.next_reset_at || null); preview.value = null; success.value = data.message; emit('saved') }
  } catch (e) {
    if (current === generation && !requestBody.value) preview.value = null
    await showFailure(e, current)
  } finally { if (current === generation) saving.value = false }
}
function close() { if (!saving.value) emit('close') }
</script>
<template>
  <el-drawer :model-value="!!service" title="下一次流量重置时间" size="min(560px, 100vw)" :close-on-click-modal="!saving" :close-on-press-escape="!saving" :show-close="!saving" @close="close">
    <p class="muted">神舟云 #{{ service?.id.slice(0, 8) }} · {{ service?.user?.username }}</p>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
    <el-alert v-if="success" :title="success" type="success" show-icon :closable="false" class="spaced" />
    <el-alert v-if="requestBody && !saving" title="上次保存结果尚未确认。可以关闭后再打开并重试确认；请暂勿刷新整个页面，确认前不能修改时间。" type="warning" show-icon :closable="false" class="spaced" />
    <el-skeleton v-if="busy && !billing" :rows="6" animated />
    <template v-if="billing">
      <dl class="billing-facts"><div><dt>当前账期</dt><dd>{{ formatDate(billing.current_cycle?.starts_at || null) }}<br>至 {{ formatDate(billing.current_cycle?.ends_at || null) }}</dd></div><div><dt>本期额度</dt><dd>{{ formatGB(billing.quota_bytes) }}</dd></div><div><dt>已用 / 剩余</dt><dd>{{ formatGB(billing.used_bytes, '暂无可靠统计') }} / {{ formatGB(billing.remaining_bytes, '待核算') }}</dd></div><div><dt>到期时间</dt><dd>{{ formatDate(billing.expires_at) }}</dd></div></dl>
      <el-alert v-if="!billing.can_modify" :title="billing.blocked_reason || '当前账期尚未准备好，暂不可修改。'" type="warning" show-icon :closable="false" />
      <template v-else><div class="billing-input"><label for="next-reset">下一次流量重置时间</label><el-date-picker id="next-reset" v-model="input" type="datetime" format="YYYY-MM-DD HH:mm" value-format="YYYY-MM-DD[T]HH:mm" placeholder="选择日期和时分" :disabled="saving || busy || !!requestBody" :show-seconds="false" /><p class="small muted">以上海时间（UTC+8）填写未来日期，精确到分钟。</p></div>
        <el-button :loading="busy" :disabled="saving || busy || !!requestBody || !planReady || !input" @click="createPreview">预览影响</el-button><el-button :disabled="saving || busy || !!requestBody" @click="refreshPlan">刷新当前计划</el-button>
        <div v-if="preview" class="preview-panel"><h3>{{ shiftLabel(preview.shift_seconds) }}</h3><p><span class="muted">原时间</span><br>{{ formatDate(preview.old_next_reset_at) }}</p><p><span class="muted">新时间</span><br>{{ formatDate(preview.next_reset_at) }}</p><h4>随后三个自然月</h4><ol><li v-for="date in preview.next_resets" :key="date">{{ formatDate(date) }}</li></ol><p class="preserve-note">本期已用 {{ formatGB(billing.used_bytes, '暂不可确认') }}、剩余 {{ formatGB(billing.remaining_bytes, '待核算') }} 保留；到新时间才开始新账期。额度、到期和使用权限保持当前设置。</p><el-alert v-if="preview.expires_before_reset" title="服务会先到期。修改重置时间不会续期或恢复服务。" type="warning" :closable="false" show-icon /><p v-if="!canSave && !saving" class="small muted">预览已失效，请重新预览影响。</p></div>
      </template>
    </template>
    <template #footer><div class="drawer-footer"><el-button :disabled="saving" @click="close">关闭</el-button><el-button v-if="billing?.can_modify || requestBody" type="primary" :loading="saving" :disabled="!canSave" @click="save">{{ requestBody && !saving ? '重试确认保存结果' : '确认修改' }}</el-button></div></template>
  </el-drawer>
</template>
