<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ApiError, errorMessage, request } from '../api'
import type { ResourceMetadata } from '../inventoryTypes'

const props = defineProps<{ item: ResourceMetadata; kind: 'cores' | 'ingresses' | 'egresses'; enabled: boolean }>()
const emit = defineEmits<{ saved: [item: ResourceMetadata] }>()
const opened = ref(false), loaded = ref(false), editable = ref(false), busy = ref(false)
const name = ref(''), notes = ref(''), revision = ref(0), error = ref(''), notice = ref('')
const baseline = ref<ResourceMetadata | null>(null), latest = ref<ResourceMetadata | null>(null)
const conflict = ref(false), preview = ref(false)
const pending = ref<{ name: string; notes?: string; revision: number; idempotency_key: string } | null>(null)
const hasNotes = computed(() => props.kind === 'cores')
const changed = computed(() => !!baseline.value && (name.value.trim() !== baseline.value.name || (hasNotes.value && notes.value.trim() !== (baseline.value.notes || ''))))
const canEdit = computed(() => props.enabled && editable.value)
const valid = computed(() => !!name.value.trim() && name.value.trim().length <= 100 && (!hasNotes.value || notes.value.trim().length <= 1000))
const path = computed(() => '/admin/' + props.kind + '/' + encodeURIComponent(props.item.id))
let generation = 0
watch([() => props.kind, () => props.item.id], () => {
  generation++; opened.value = false; loaded.value = false; editable.value = false; busy.value = false
  name.value = ''; notes.value = ''; revision.value = 0; error.value = ''; notice.value = ''
  baseline.value = null; latest.value = null; conflict.value = false; preview.value = false; pending.value = null
}, { immediate: true, flush: 'sync' })
function verifyItem(item: ResourceMetadata | undefined): ResourceMetadata {
  if (!item || item.id !== props.item.id || typeof item.name !== 'string' || !Number.isSafeInteger(item.revision) || item.revision < 1 || (hasNotes.value && typeof item.notes !== 'string')) throw new ApiError(0, 'INVALID_RESPONSE', '未能确认资料回执，请重试原请求。')
  return item
}
async function readItem(target: string) {
  return request<{ item: ResourceMetadata; read_only: boolean; capabilities: { edit: boolean } }>(target)
}
function useItem(item: ResourceMetadata) { baseline.value = { ...item }; name.value = item.name; notes.value = item.notes || ''; revision.value = item.revision }
async function openEditor() {
  if (busy.value || !props.enabled) return
  const current = generation, target = path.value; opened.value = true; busy.value = true; error.value = ''
  try {
    const data = await readItem(target)
    if (current !== generation) return
    useItem(verifyItem(data.item)); editable.value = data.read_only === false && data.capabilities?.edit === true; loaded.value = true
  } catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}
function previewChanges() {
  if (!canEdit.value || busy.value || pending.value || conflict.value || !valid.value || !changed.value) return
  error.value = ''; notice.value = ''; preview.value = true
}
async function save() {
  if (!canEdit.value || busy.value || conflict.value || (!pending.value && (!preview.value || !valid.value || !changed.value))) return
  const current = generation, target = path.value
  pending.value ||= { name: name.value.trim(), ...(hasNotes.value ? { notes: notes.value.trim() } : {}), revision: revision.value, idempotency_key: crypto.randomUUID() }
  busy.value = true; error.value = ''; notice.value = ''
  let receiptAccepted = false
  try {
    const result = await request<{ item: ResourceMetadata; replayed: boolean; message: string }>(target, 'PATCH', pending.value)
    if (current !== generation) return
    let saved = verifyItem(result.item)
    if (typeof result.replayed !== 'boolean') throw new ApiError(0, 'INVALID_RESPONSE', '未能确认资料回执，请重试原请求。')
    receiptAccepted = true
    if (result.replayed) {
      const data = await readItem(target)
      if (current !== generation) return
      saved = verifyItem(data.item); editable.value = data.read_only === false && data.capabilities?.edit === true
    }
    useItem(saved); pending.value = null; preview.value = false; latest.value = null
    notice.value = result.replayed ? '资料已保存，已读取当前版本。' : '资料已保存。'; emit('saved', saved)
  } catch (e) {
    if (current !== generation) return
    error.value = errorMessage(e)
    // 通信不确定、写入竞争和回执刷新失败保留原请求，避免生成第二次修改。
    if (!receiptAccepted && e instanceof ApiError && e.status >= 400 && e.status < 500 && e.code !== 'write_conflict' && e.code !== 'INVALID_RESPONSE') {
      pending.value = null; preview.value = false; conflict.value = e.code === 'revision_conflict'
    }
  } finally { if (current === generation) busy.value = false }
}
async function readLatest() {
  if (busy.value || !conflict.value || !props.enabled) return
  const current = generation, target = path.value; busy.value = true; error.value = ''
  try {
    const data = await readItem(target)
    if (current !== generation) return
    latest.value = verifyItem(data.item); editable.value = data.read_only === false && data.capabilities?.edit === true
  } catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}
function adoptLatest() {
  if (!latest.value || busy.value || !canEdit.value) return
  baseline.value = { ...latest.value }; revision.value = latest.value.revision; conflict.value = false; latest.value = null; error.value = ''; preview.value = false
}
onUnmounted(() => { generation++ })
</script>
<template>
  <section class="resource-metadata-editor" aria-label="资源资料编辑">
    <el-button v-if="!loaded" :loading="busy" :disabled="busy || !enabled" @click="openEditor">{{ opened ? '重新读取资料' : '编辑资料' }}</el-button>
    <template v-if="loaded">
      <h4>资源资料</h4>
      <el-form label-position="top" :disabled="!canEdit || busy || preview || !!pending" @submit.prevent="previewChanges">
        <el-form-item label="显示别名"><el-input v-model="name" maxlength="100" aria-label="资源别名" /></el-form-item>
        <el-form-item v-if="hasNotes" label="登记备注"><el-input v-model="notes" type="textarea" :rows="2" maxlength="1000" show-word-limit aria-label="资源备注" /></el-form-item>
      </el-form>
      <p class="small muted">仅修改显示资料，不改变连接或运行配置。请勿填写密码和订阅链接。</p>
      <div v-if="preview" class="metadata-preview" aria-label="资料修改预览">
        <h4>确认资料修改</h4><dl><div><dt>显示别名</dt><dd><span>{{ baseline?.name }}</span> → <strong>{{ name.trim() }}</strong></dd></div><div v-if="hasNotes"><dt>登记备注</dt><dd><span>{{ baseline?.notes || '无' }}</span> → <strong>{{ notes.trim() || '无' }}</strong></dd></div></dl>
        <div v-if="!pending" class="metadata-actions"><el-button :disabled="busy" @click="preview = false">返回修改</el-button><el-button type="primary" :loading="busy" :disabled="busy || !canEdit" @click="save">确认保存资料</el-button></div>
      </div>
      <div v-if="conflict" class="metadata-conflict"><el-button :loading="busy" :disabled="busy || !enabled" @click="readLatest">读取当前版本，保留输入</el-button><template v-if="latest"><p>当前别名：{{ latest.name }}</p><p v-if="hasNotes">当前备注：{{ latest.notes || '无' }}</p><el-button :disabled="busy || !canEdit" @click="adoptLatest">核对后采用最新版本，保留我的输入</el-button></template></div>
      <div class="metadata-actions"><el-button v-if="!preview && !pending" type="primary" :disabled="!canEdit || busy || conflict || !valid || !changed" @click="previewChanges">预览修改</el-button><el-button v-if="pending" type="primary" :loading="busy" :disabled="!canEdit || busy" @click="save">重试原保存请求</el-button></div>
      <p v-if="pending && !busy" class="small muted" role="status">保存结果尚未确认。输入已保留，请重试原保存请求。</p>
    </template>
    <el-alert v-if="error" :title="error" type="error" :closable="false" /><p v-if="notice" role="status">{{ notice }}</p><p v-if="!enabled || (loaded && !editable)" class="small muted">此环境未开放资料修改。</p>
  </section>
</template>
<style scoped>
.resource-metadata-editor{min-width:0;margin:16px 0;padding:0 16px;overflow-wrap:anywhere}.resource-metadata-editor h4{margin:12px 0;font-size:15px}.metadata-preview{border:1px solid var(--border,#dfe6ed);border-radius:8px;padding:12px;background:var(--surface,#fff)}.metadata-preview dl{margin:0}.metadata-preview dt{font-size:12px;color:var(--muted,#687887)}.metadata-preview dd{margin:4px 0 12px;white-space:pre-wrap}.metadata-actions{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.metadata-actions :deep(.el-button + .el-button){margin-left:0}.resource-metadata-editor :deep(.el-button){max-width:100%;height:auto;min-height:32px;white-space:normal;padding:8px 12px}.resource-metadata-editor :deep(.el-button > span){min-width:0;white-space:normal;text-align:left;line-height:1.5}.metadata-conflict{margin-top:12px}.small{line-height:1.6}
@media(max-width:420px){.resource-metadata-editor{padding:0 12px}.metadata-preview{padding:10px}}
</style>
