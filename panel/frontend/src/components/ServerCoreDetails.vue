<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { formatDate } from '../display'
import type { CoreMetadata } from '../inventoryTypes'
const props = defineProps<{ cores: { items: CoreMetadata[]; truncated: boolean } }>()
const selectedId = ref<string | null>(null)
const selected = computed(() => props.cores.items.find(core => core.id === selectedId.value) || null)
const ownership: Record<string, string> = { unknown: '归属未确认', observed_only: '仅观测', verified: '归属已核对' }
watch(() => props.cores.items, items => { if (selectedId.value && !items.some(item => item.id === selectedId.value)) selectedId.value = null })
</script>
<template>
  <section aria-label="核心实例"><h2>核心实例</h2><p class="small muted">查看实际登记的版本、配置归属与关联节点。运行控制尚未接管。</p>
    <p v-if="!cores.items.length" class="quality-note">暂无已核对的核心实例。需从当前服务器核对实际实例和配置所有者，再通过资源导入登记。</p>
    <div class="core-list"><button v-for="core in cores.items" :key="core.id" :aria-pressed="selectedId === core.id" @click="selectedId = core.id"><span><strong>{{ core.name }}</strong><small>{{ core.core_type }} · {{ core.registered_version || '版本未知' }}</small></span><span>资料 →</span></button></div>
    <p v-if="cores.truncated" class="small muted">仅展示前 {{ cores.items.length }} 个核心登记。</p>
    <article v-if="selected" class="core-details" aria-label="核心资料"><h3>{{ selected.name }}</h3><dl><div><dt>实例标识</dt><dd>{{ selected.instance_key }}</dd></div><div><dt>核心类型</dt><dd>{{ selected.core_type }}</dd></div><div><dt>登记版本</dt><dd>{{ selected.registered_version || '未知' }}</dd></div><div><dt>配置所有者</dt><dd>{{ selected.configuration_owner || '未知' }}</dd></div><div><dt>服务单元</dt><dd>{{ selected.service_unit || '未知' }}</dd></div><div><dt>归属核对</dt><dd>{{ ownership[selected.owner_state || 'unknown'] || '归属未确认' }}</dd></div><div><dt>证据来源</dt><dd>{{ selected.evidence_source || '未知' }}</dd></div><div><dt>证据时间</dt><dd>{{ selected.evidence_observed_at ? formatDate(selected.evidence_observed_at) : '未知' }}</dd></div><div><dt>配置版本</dt><dd>{{ selected.configuration_version || '未知' }}</dd></div><div><dt>关联端点数</dt><dd>{{ selected.ingress_ids.length }}</dd></div></dl><p v-if="selected.notes">{{ selected.notes }}</p><p v-if="!selected.association_valid" class="quality-note">端点关联与所属服务器不一致，需重新核对。</p><p class="small muted">登记版本不等于已核验的运行版本。配置编辑、重启和升级须完成候选检查、影响预览和执行器接管后开放。</p></article>
  </section>
</template>
<style scoped>
h2{font-size:18px}.core-list{display:grid;gap:10px;margin-top:16px}.core-list button{display:flex;justify-content:space-between;align-items:center;gap:12px;text-align:left;background:#fff;border:1px solid #e6ebf3;border-radius:10px;padding:14px;color:inherit;cursor:pointer;font:inherit;min-width:0}.core-list button[aria-pressed=true]{border-color:var(--el-color-primary,#365ccb);background:#f4f7fe}.core-list small{display:block;color:#75849b;margin-top:6px}.core-details{padding:18px;background:#f7f9fd;border-radius:12px;margin-top:20px;overflow-wrap:anywhere}dl{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}dt{font-size:12px;color:#75849b}dd{margin:6px 0 0}@media(max-width:520px){dl{grid-template-columns:1fr}}
</style>
