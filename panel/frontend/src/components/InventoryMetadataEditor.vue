<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { ApiError, errorMessage, request } from '../api'
import type { ResourceMetadata } from '../inventoryTypes'

const props = defineProps<{ item: ResourceMetadata; kind: 'servers' | 'lines'; enabled: boolean }>()
const emit = defineEmits<{ saved: [item: ResourceMetadata] }>()
const name = ref(''), notes = ref(''), revision = ref(0), busy = ref(false), error = ref(''), notice = ref('')
const pending = ref<{ name: string; notes: string; revision: number; idempotency_key: string } | null>(null)
const conflict = ref(false), latest = ref<ResourceMetadata | null>(null)
let generation = 0
watch(() => props.item.id, () => {
  generation++; name.value = props.item.name; notes.value = props.item.notes || ''; revision.value = props.item.revision
  busy.value = false; error.value = ''; notice.value = ''; pending.value = null; conflict.value = false; latest.value = null
}, { immediate: true })
async function save() {
  if (busy.value || !props.enabled || conflict.value) return
  const current = generation, id = props.item.id
  pending.value ||= { name: name.value.trim(), notes: notes.value.trim(), revision: revision.value, idempotency_key: crypto.randomUUID() }
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const result = await request<{ item: ResourceMetadata; replayed: boolean; message: string }>('/admin/' + props.kind + '/' + encodeURIComponent(id), 'PATCH', pending.value)
    if (current !== generation) return
    if (result.replayed) {
      const data = await request<{ server?: ResourceMetadata; line?: ResourceMetadata }>('/admin/' + props.kind + '/' + encodeURIComponent(id))
      if (current !== generation) return
      result.item = data.server || data.line || result.item
    }
    pending.value = null; revision.value = result.item.revision; name.value = result.item.name; notes.value = result.item.notes
    notice.value = result.message; emit('saved', result.item)
  } catch (e) {
    if (current !== generation) return
    error.value = errorMessage(e)
    // 明确拒绝可重新编辑；通信不确定或写入竞争保持同一个请求标识。
    if (e instanceof ApiError && e.status >= 400 && e.status < 500 && e.code !== 'write_conflict') {
      pending.value = null; conflict.value = e.code === 'revision_conflict'
    }
  } finally { if (current === generation) busy.value = false }
}
async function readLatest() {
  if (busy.value) return
  const current = generation; busy.value = true; error.value = ''
  try {
    const data = await request<{ server?: ResourceMetadata; line?: ResourceMetadata }>('/admin/' + props.kind + '/' + encodeURIComponent(props.item.id))
    if (current === generation) latest.value = data.server || data.line || null
  } catch (e) { if (current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) busy.value = false }
}
function adoptLatest() {
  if (!latest.value) return
  revision.value = latest.value.revision; conflict.value = false; latest.value = null; error.value = ''
}
onUnmounted(() => { generation++ })
</script>
<template>
  <section class="metadata-editor" aria-label="资源资料编辑">
    <h2>资源资料</h2>
    <el-form label-position="top" :disabled="!enabled || busy || !!pending" @submit.prevent="save">
      <el-form-item label="显示别名"><el-input v-model="name" maxlength="100" aria-label="资源别名" /></el-form-item>
      <el-form-item label="登记备注"><el-input v-model="notes" type="textarea" :rows="2" maxlength="1000" show-word-limit aria-label="资源备注" /></el-form-item>
    </el-form>
    <p class="small muted">仅修改显示资料，不修改节点地址、身份、权限或运行配置；请勿填写密码和订阅链接。</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <p v-if="notice" role="status">{{ notice }}</p>
    <div v-if="conflict" class="spaced"><el-button :loading="busy" @click="readLatest">读取当前版本，保留输入</el-button><template v-if="latest"><p>当前别名：{{ latest.name }}；备注：{{ latest.notes || '无' }}；版本 {{ latest.revision }}</p><el-button @click="adoptLatest">核对后采用最新版本，保留我的输入</el-button></template></div>
    <el-button type="primary" class="spaced" :loading="busy" :disabled="!enabled || busy || conflict || !name.trim()" @click="save">{{ pending ? '重试原保存请求' : '保存资料' }}</el-button>
    <span v-if="!enabled" class="small muted"> 此环境未开放资料修改。</span>
  </section>
</template>
<style scoped>.metadata-editor{margin:20px 0;padding-bottom:22px;border-bottom:1px solid #e7ecf4}.metadata-editor h2{font-size:18px}</style>
