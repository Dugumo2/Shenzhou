<script setup lang="ts">
import { computed } from 'vue'
import type { MeteringDiagnostics } from '../types'
import { formatDate, formatGB } from '../display'
import RefreshControl from './RefreshControl.vue'

const props = defineProps<{ data: MeteringDiagnostics | null; loading?: boolean; error?: string }>()
const emit = defineEmits<{ refresh: [] }>()
const reasonLabels: Record<string, string> = {
  persisted_gap: '计量已标记缺口，需要核对对应采样记录。',
  overlapping_cycles: '存在重叠账期，需要核对账期归属。',
  ambiguous_streams: '采样流归属不明确，不能确认完整覆盖。',
  no_current_cycle: '尚未取得当前账期。',
  no_active_identities: '当前没有启用的计量身份。',
  missing_complete_evidence: '尚未取得完整覆盖证据。',
  partial_observed_evidence: '已记录部分采样金额，尚不能确认完整区间覆盖；这本身不证明实际断采。',
  future_timestamp: '采样时刻晚于当前时间，需要核对时钟。',
  outside_current_cycle: '采样时刻不在当前账期内。',
  stale_evidence: '采样记录已过期，需要核对采集是否持续。',
}
const qualityLabels: Record<string, string> = { measured: '已确认', gap: '覆盖待核对', stale: '更新延迟', unknown: '尚无可靠记录', partial_observed: '部分已观测', complete: '完整覆盖', partial: '部分覆盖', simulated: '隔离验证' }
const coverageLabels: Record<string, string> = { complete: '完整覆盖', partial: '部分覆盖', missing: '缺少覆盖记录', unavailable: '范围数据暂不可用' }
const coverageNotes: Record<string, string> = {
  sampling_pending: '当前开放时段仍等待后续采样；正常采样尾部不能直接判为断采。',
  partial_coverage: '当前范围尚未获得完整覆盖证明，请结合采样证据核对。',
  unallocated_intervals: '部分记录暂未分配到时间桶；计入金额与时间归属需分别核对。',
  boundary_pending: '存在待确认的时间边界，暂不能确认该范围的完整归属。',
  missing_history: '该范围没有可用的时间记录，不以当前累计推算历史。',
  overlapping_intervals: '采样区间重叠，当前范围暂不可确认。',
}
const reasons = computed(() => [...new Set(props.data?.metering.reason_codes ?? [])].map(code => reasonLabels[code] || '存在未识别的计量状态，请核对服务端诊断。'))
function observedDate(value: string | null) { return value ? formatDate(value) : '未取得' }
</script>

<template>
  <section class="metering-diagnostics" aria-labelledby="metering-diagnostics-title" :aria-busy="!!loading">
    <div class="diagnostics-heading">
      <div><h2 id="metering-diagnostics-title">计量诊断</h2><p class="muted small">仅供管理员核对采样与覆盖；重新读取不会触发采集。</p></div>
      <RefreshControl :loading="!!loading" :error="error || ''" :last-read-at="data?.generated_at || null" @refresh="emit('refresh')" />
    </div>
    <p v-if="error" class="diagnostics-error" role="alert">计量诊断读取失败。{{ data ? '已保留上次结果，请重试。' : '请重试。' }}</p>
    <p v-if="loading && !data" class="muted" role="status">正在读取计量诊断…</p>
    <p v-else-if="!data && !error" class="muted">尚未读取计量诊断。</p>
    <template v-if="data">
      <dl class="diagnostics-facts">
        <div class="wide-fact"><dt>服务编号</dt><dd>{{ data.service_id }}</dd></div>
        <div><dt>服务登记状态</dt><dd>{{ data.service_status.label }}</dd></div>
        <div><dt>计量质量</dt><dd>{{ qualityLabels[data.metering.quality] || '待核对' }}</dd></div>
        <div><dt>采样证据</dt><dd>{{ qualityLabels[data.metering.evidence_quality] || '待核对' }}</dd></div>
        <div><dt>实际采样时刻（上海）</dt><dd>{{ observedDate(data.metering.updated_at) }}</dd></div>
        <div><dt>缺口标记</dt><dd>{{ data.metering.persisted_gap ? '已标记，需核对原因' : '未标记（不代表完整覆盖）' }}</dd></div>
        <div><dt>范围覆盖</dt><dd>{{ coverageLabels[data.coverage.state] || '待核对' }}</dd></div>
        <div><dt>范围开始（上海）</dt><dd>{{ observedDate(data.coverage.range_start) }}</dd></div>
        <div><dt>范围结束（上海）</dt><dd>{{ observedDate(data.coverage.range_end) }}</dd></div>
        <div><dt>未分桶扣减</dt><dd>{{ formatGB(data.coverage.unallocated_charged_bytes, '暂不可确认') }}</dd></div>
        <div><dt>待确认边界数</dt><dd>{{ data.coverage.boundary_pending_count ?? '暂不可确认' }}</dd></div>
      </dl>
      <p v-if="data.coverage.message_code" class="muted small">{{ coverageNotes[data.coverage.message_code] || '范围覆盖有额外待核对状态。' }}</p>
      <h3>采样身份</h3>
      <dl class="diagnostics-facts identity-facts">
        <div><dt>启用身份</dt><dd>{{ data.metering.active_identity_count }}</dd></div>
        <div><dt>有证据身份</dt><dd>{{ data.metering.evidence_identity_count }}</dd></div>
        <div><dt>未验证身份</dt><dd>{{ data.metering.unverified_identity_count }}</dd></div>
        <div><dt>归属不明身份</dt><dd>{{ data.metering.ambiguous_identity_count }}</dd></div>
        <div><dt>部分观测身份</dt><dd>{{ data.metering.partial_observed_identity_count }}</dd></div>
      </dl>
      <h3>原因与核对方向</h3>
      <ul v-if="reasons.length" class="diagnostics-notes"><li v-for="(reason, index) in reasons" :key="index">{{ reason }}</li></ul>
      <p v-else class="muted small">未返回额外原因；不据此推断采样完整。</p>
      <h3>一次性历史衔接</h3>
      <dl v-if="data.historical_baseline.state === 'applied'" class="diagnostics-facts">
        <div><dt>当期衔接量</dt><dd>{{ formatGB(data.historical_baseline.charged_bytes) }}</dd></div>
        <div><dt>来源观测时刻（上海）</dt><dd>{{ observedDate(data.historical_baseline.source_observed_at) }}</dd></div>
      </dl>
      <p class="muted small">{{ data.historical_baseline.state === 'applied' ? '已计入当期累计，不分摊到历史时间图，也不滚入下一期。' : '当前账期没有一次性历史衔接量。' }}</p>
      <h3>能力验收边界</h3>
      <dl class="diagnostics-facts">
        <div><dt>无损尾部计量</dt><dd>{{ data.limitations.lossless_tail_verified ? '已验证' : '尚未验证' }}</dd></div>
        <div><dt>硬限额执行</dt><dd>{{ data.limitations.hard_limit_verified ? '已验证' : '尚未验证' }}</dd></div>
        <div><dt>撤销执行</dt><dd>{{ data.limitations.revocation_verified ? '已验证' : '尚未验证' }}</dd></div>
      </dl>
      <p class="muted small">能力未验收与具体日期缺采原因分别核对，不能互相代替。</p>
    </template>
  </section>
</template>

<style scoped>
.metering-diagnostics { min-width: 0; }
.diagnostics-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
.diagnostics-heading h2 { margin: 0; font-size: 20px; }
.metering-diagnostics h3 { margin: 24px 0 12px; font-size: 15px; }
.diagnostics-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px 24px; margin: 16px 0; }
.diagnostics-facts div { min-width: 0; }
.diagnostics-facts dt { color: #78869b; font-size: 12px; margin-bottom: 6px; }
.diagnostics-facts dd { margin: 0; overflow-wrap: anywhere; font-size: 14px; }
.wide-fact { grid-column: 1 / -1; }
.diagnostics-notes { padding-left: 20px; line-height: 1.7; font-size: 13px; overflow-wrap: anywhere; }
.diagnostics-error { color: #b42318; line-height: 1.6; font-size: 13px; }
@media (max-width: 420px) { .diagnostics-facts { grid-template-columns: minmax(0, 1fr); } }
</style>
