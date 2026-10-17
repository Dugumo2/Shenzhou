<script setup lang="ts">
import { computed } from 'vue'
import type { LinePathProjection, LineSegmentCapability } from '../inventoryTypes'
import { formatDate } from '../display'
import { effectiveFreshness, useInventoryClock } from '../inventoryStatus'

const props = defineProps<{ path?: LinePathProjection }>()
const now = useInventoryClock()
const kindLabels = { registered_association: '已有入口与出口关联', observed_path: '已观测配置路径', single_node: '单节点资源' }
const relations = { registered: '登记关联 · 中间跳段未核实', observed: '有配置关系证据 · 流量能力另验', unknown: '连接关系未知' }
const points = { ingress: '入口节点', egress: '出口资源', core: '核心实例', unknown: '未知目标' }
const capabilities = ['tcp', 'udp', 'dns'] as const
const cycle = computed(() => {
  const edges = new Map<string, string[]>()
  for (const segment of props.path?.segments || []) {
    const from = segment.from.kind + ':' + segment.from.id, to = segment.to.kind + ':' + segment.to.id
    edges.set(from, [...(edges.get(from) || []), to])
  }
  const visiting = new Set<string>(), visited = new Set<string>()
  function visit(node: string): boolean {
    if (visiting.has(node)) return true
    if (visited.has(node)) return false
    visiting.add(node)
    if ((edges.get(node) || []).some(visit)) return true
    visiting.delete(node); visited.add(node)
    return false
  }
  return [...edges.keys()].some(visit)
})
function capabilityLabel(value: LineSegmentCapability) {
  if (value.state === 'unknown') return '未知 / 未验证'
  const freshness = effectiveFreshness(value.freshness, value.expires_at || undefined, now.value)
  const result = { supported: '登记支持', unsupported: '登记不支持', pass: '检查通过', fail: '检查失败', timeout: '检查超时' }[value.state]
  if (freshness !== 'fresh') return result + ' · ' + ({ stale: '证据已过期', target_changed: '目标已变化', unknown: '时效未知' }[freshness] || '时效未知')
  return result + ' · 有效期内'
}
</script>

<template>
  <section class="line-path-diagram" aria-label="线路路径与分段能力">
    <header><h2>{{ path ? kindLabels[path.kind] : '路径资料待接入' }}</h2><p class="small muted">只展示有依据的关系；虚线表示登记或未知连接，不表示检查通过。</p></header>
    <p v-if="!path" role="status">当前响应尚未提供路径证据，不能据入口与出口名称推断中间跳段。</p>
    <template v-else>
      <p class="path-source small muted">来源 {{ path.source === 'inventory_registration' ? '现有资源登记' : path.source || '未提供' }} · 路径观测 {{ path.observed_at ? formatDate(path.observed_at) : '尚无完整路径观测' }}</p>
      <p v-if="cycle" role="alert" class="path-warning">路径关系出现环路，不能视作可用路径。请先核对关系；不会提供直出补偿。</p>
      <p v-if="path.kind === 'single_node'" class="single-node">这是单节点资源，无需包装成多跳线路。{{ path.segments[0]?.from.name || '' }}</p>
      <div v-else class="path-segments">
        <article v-for="(segment, index) in path.segments" :key="segment.id" class="path-segment" :class="{ uncertain: segment.relation !== 'observed' || cycle }">
          <p class="segment-heading">关联 {{ index + 1 }} · {{ relations[segment.relation] }}</p>
          <p v-if="segment.evidence_source" class="small muted">证据 {{ segment.evidence_source }} · {{ segment.evidence_observed_at ? formatDate(segment.evidence_observed_at) : '时间未知' }}</p>
          <div class="path-connection" :aria-label="segment.from.name + '至' + segment.to.name + '，' + relations[segment.relation]">
            <div class="path-point"><span>{{ points[segment.from.kind] }}</span><strong>{{ segment.from.name }}</strong><small>#{{ segment.from.id }}</small></div>
            <div class="path-link" aria-hidden="true"><svg viewBox="0 0 100 32" fill="none"><path d="M3 16H95M86 8L95 16L86 24" /></svg><span>{{ segment.relation === 'observed' ? '已观测' : '待核实' }}</span></div>
            <div class="path-point"><span>{{ points[segment.to.kind] }}</span><strong>{{ segment.to.name }}</strong><small>#{{ segment.to.id }}</small></div>
          </div>
          <dl class="segment-facts"><div><dt>该段协议</dt><dd>{{ segment.protocol || '未知；节点协议另列' }}</dd></div><div><dt>该段执行核心</dt><dd>{{ segment.core ? segment.core.name + ' · ' + segment.core.core_type : '未知；不按同机或名称推断' }}</dd></div><div><dt>认证引用</dt><dd>{{ segment.authentication === 'registered' ? '已登记引用，未展示认证材料' : '该段尚未核实' }}</dd></div><div><dt>失败行为</dt><dd>{{ segment.failure_behavior === 'fail_closed_required' ? '已登记失败关闭要求，执行未验证' : '该段尚未核实' }}</dd></div></dl>
          <div class="segment-capabilities"><div v-for="name in capabilities" :key="name"><strong>{{ name.toUpperCase() }}</strong><span>{{ capabilityLabel(segment[name]) }}</span><small v-if="segment[name].source">来源 {{ segment[name].source }}</small><time v-if="segment[name].observed_at" :datetime="segment[name].observed_at!">检查 {{ formatDate(segment[name].observed_at) }}</time></div></div>
        </article>
      </div>
      <ul v-if="path.warnings.length" class="path-boundaries"><li v-for="warning in path.warnings" :key="warning">{{ warning }}</li></ul>
    </template>
  </section>
</template>

<style scoped>
.line-path-diagram{min-width:0;margin:24px 0}.line-path-diagram h2{font-size:18px;margin-bottom:10px}.line-path-diagram p{line-height:1.7;overflow-wrap:anywhere}.path-segments{display:grid;gap:16px}.path-segment{border:1px solid var(--sz-border,#e6ebf3);border-radius:14px;padding:18px;min-width:0}.segment-heading{font-size:12px;color:var(--sz-text-secondary,#63748f);margin:0 0 16px}.path-connection{display:grid;grid-template-columns:minmax(0,1fr) 80px minmax(0,1fr);gap:10px;align-items:center}.path-point{background:var(--sz-bg,#f6f8fc);border:1px solid var(--sz-border,#e6ebf3);border-radius:10px;padding:12px;display:grid;gap:6px;min-width:0;overflow-wrap:anywhere}.path-point span,.path-point small{font-size:12px;color:var(--sz-text-secondary,#63748f)}.path-link{text-align:center;color:var(--sz-text-secondary,#63748f);font-size:11px}.path-link svg{width:100%;height:30px;stroke:currentColor;stroke-width:1.5}.uncertain .path-link path{stroke-dasharray:5 4}.segment-facts{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:20px 0}dt{font-size:12px;color:var(--sz-text-secondary,#63748f)}dd{margin:5px 0 0;font-size:13px;overflow-wrap:anywhere;line-height:1.6}.segment-capabilities{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.segment-capabilities>div{display:flex;flex-direction:column;gap:7px;background:var(--sz-bg,#f6f8fc);padding:12px;border-radius:8px;font-size:12px;overflow-wrap:anywhere}.segment-capabilities strong{font-size:13px}.segment-capabilities small,.segment-capabilities time{color:var(--sz-text-secondary,#63748f);font-size:11px}.path-boundaries{padding-left:20px;color:var(--sz-text-secondary,#63748f);font-size:12px;line-height:1.8}.path-warning{border-left:3px solid #b75f20;padding:10px;background:#fff7ed;color:#8c4417}.single-node{padding:16px;border:1px solid var(--sz-border,#e6ebf3);border-radius:10px}@media(max-width:520px){.path-segment{padding:14px}.path-connection{grid-template-columns:1fr}.path-link svg{width:50px;height:50px;transform:rotate(90deg)}.path-link{display:flex;align-items:center;justify-content:center;gap:6px}.segment-facts,.segment-capabilities{grid-template-columns:1fr}}
</style>
