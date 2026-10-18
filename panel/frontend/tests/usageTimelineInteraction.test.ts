import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as display from '../src/display.ts'
import * as timeseries from '../src/usageTimeseries.ts'
import type { CalendarPeriod, UsageOverviewData } from '../src/usage-types.ts'

// 编译真实模板并触发真实事件处理器；宿主不模拟浏览器布局，视觉另行验收。
const source = readFileSync(new URL('../src/components/UsageTimeline.vue', import.meta.url), 'utf8')
const script = compileScript(parse(source).descriptor, { id: 'usage-interaction', inlineTemplate: true })
const exports: Record<string, any> = {}
const code = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
new Function('require', 'exports', code)((name: string) => {
  if (name === 'vue') return vue
  if (name === '../display') return display
  if (name === '../usageTimeseries') return timeseries
  throw new Error('未声明的测试依赖：' + name)
}, exports)

function fixture(period: CalendarPeriod = '7d'): UsageOverviewData {
  const buckets = timeseries.emptyCalendar(period, '2026-10-18T12:00:00+08:00')
  Object.assign(buckets[0]!, { state: 'complete', charged_bytes: '12345678901', upload_bytes: '123456789', download_bytes: '10000000000' })
  Object.assign(buckets[1]!, { state: 'complete', charged_bytes: '0', upload_bytes: '0', download_bytes: '0' })
  Object.assign(buckets[2]!, { state: 'partial', charged_bytes: '1', upload_bytes: '1', download_bytes: '0' })
  Object.assign(buckets[3]!, { state: 'future' })
  return { service_id: 'interaction-fixture', generated_at: '2026-10-18T12:00:00+08:00', period,
    source_type: 'entitlement', time_zone: 'Asia/Shanghai', current_cycle: null,
    summary: { quota_bytes: '1000000000000', quota_state: 'applied', charged_bytes: null, upload_bytes: null, download_bytes: null, remaining_bytes: null, next_reset_at: null },
    quality: { state: 'gap', message: '', collected_at: '2026-10-18T12:00:00+08:00' },
    history: { kind: 'confirmed_ledger_postings', date_basis: 'created_at', range_start: null, range_end: null, totals: null, record_count: 0, excluded_record_count: 0, days: [], day_limit: 90, days_truncated: false, message: '' },
    timeseries: { service_id: 'interaction-fixture', period, range_start: buckets[0]!.start, range_end: buckets.at(-1)!.end, as_of: '2026-10-18T12:00:00+08:00', summary_revision: 'r1', time_zone: 'Asia/Shanghai', granularity: period === 'year' ? 'month' : 'day', buckets,
      totals: { state: 'partial', charged_bytes: '12345678902', upload_bytes: '123456790', download_bytes: '10000000000', unallocated_charged_bytes: '0' }, unallocated: [], unallocated_next_cursor: null, boundary_pending_count: 0, message_code: 'partial_coverage' },
  }
}
type Host = { tag: string; text: string; props: Record<string, any>; children: Host[]; parent: Host | null }
const node = (tag: string, text = ''): Host => ({ tag, text, props: {}, children: [], parent: null })
function walk(item: Host): Host[] { return [item, ...item.children.flatMap(walk)] }
function textOf(item: Host): string { return item.text + item.children.map(textOf).join('') }
function mount(data: UsageOverviewData | null = fixture(), period: CalendarPeriod = '7d', loading = false) {
  const state = vue.reactive({ data, period, loading })
  const renderer = vue.createRenderer<Host, Host>({
    createElement: tag => node(tag), createText: text => node('#text', text), createComment: text => node('#comment', text),
    insert: (child, parent, anchor) => { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = parent; const index = anchor ? parent.children.indexOf(anchor) : -1; if (index < 0) parent.children.push(child); else parent.children.splice(index, 0, child) },
    remove: child => { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null },
    setText: (item, value) => { item.text = value }, setElementText: (item, value) => { item.text = value; item.children = [] }, parentNode: item => item.parent,
    nextSibling: item => item.parent?.children[item.parent.children.indexOf(item) + 1] || null, patchProp: (item, key, _old, value) => { item.props[key] = value },
  })
  const app = renderer.createApp({ setup: () => () => vue.h(exports.default, state) })
  app.component('ElButton', { setup: (_: any, { attrs, slots }: any) => () => vue.h('button', attrs, slots.default?.()) })
  app.component('ElDrawer', { props: ['modelValue'], setup: (p: any, { slots }: any) => () => p.modelValue ? vue.h('aside', slots.default?.()) : null })
  app.component('ElRadioGroup', { props: ['modelValue'], emits: ['update:modelValue'], setup: (_: any, { emit, slots }: any) => () => vue.h('div', { 'data-metric-control': true, onChange: (value: string) => emit('update:modelValue', value) }, slots.default?.()) })
  app.component('ElRadioButton', { props: ['value'], setup: (_: any, { slots }: any) => () => vue.h('span', slots.default?.()) })
  const host = node('root'); app.mount(host)
  return { state, nodes: () => walk(host), bars: () => walk(host).filter(n => n.props['data-testid'] === 'usage-time-bar'), find: (id: string) => walk(host).find(n => n.props['data-testid'] === id), text: () => textOf(host), unmount: () => app.unmount() }
}

test('鼠标悬浮与键盘聚焦使用同一卡，保留精确字节且无原生title', async () => {
  const ui = mount()
  try {
    const bar = ui.bars()[0]!
    assert.equal(bar.props.title, undefined)
    bar.props.onPointerenter({ pointerType: 'mouse' }); await vue.nextTick()
    assert.equal(textOf(ui.find('usage-point-date')!), '2026-10-12')
    assert.equal(textOf(ui.find('usage-point-metric')!), '套餐消耗')
    assert.equal(textOf(ui.find('usage-point-value')!), '12.34 GB')
    assert.equal(textOf(ui.find('usage-point-exact')!), '12,345,678,901 字节')
    assert.equal(ui.find('usage-point-card')!.props['data-pinned'], false)
    bar.props.onPointerleave(); await vue.nextTick(); assert.equal(ui.find('usage-point-card'), undefined)
    bar.props.onFocus(); await vue.nextTick(); assert.ok(ui.find('usage-point-card'))
    assert.match(bar.props.class, /selected/)
    bar.props.onBlur(); await vue.nextTick(); assert.equal(ui.find('usage-point-card'), undefined)
  } finally { ui.unmount() }
})

test('触屏点击固定、悬浮不覆盖、再次点击及Escape取消，零与缺失未来不混淆', async () => {
  const ui = mount()
  try {
    const bars = ui.bars()
    bars[1]!.props.onPointerenter({ pointerType: 'touch' }); await vue.nextTick(); assert.equal(ui.find('usage-point-card'), undefined)
    bars[1]!.props.onClick(); await vue.nextTick()
    assert.equal(bars[1]!.props['aria-pressed'], true); assert.equal(textOf(ui.find('usage-point-value')!), '0 GB')
    bars[0]!.props.onPointerenter({ pointerType: 'mouse' }); await vue.nextTick(); assert.equal(textOf(ui.find('usage-point-value')!), '0 GB')
    bars[1]!.props.onClick(); await vue.nextTick(); assert.equal(ui.find('usage-point-card'), undefined)
    bars[2]!.props.onClick(); await vue.nextTick(); assert.equal(textOf(ui.find('usage-point-value')!), '< 0.01 GB'); assert.match(ui.text(), /1 字节/)
    ui.nodes().find(n => n.props.onKeydown)!.props.onKeydown({ key: 'Escape' }); await vue.nextTick(); assert.equal(ui.find('usage-point-card'), undefined)
    bars[3]!.props.onClick(); await vue.nextTick(); assert.equal(textOf(ui.find('usage-point-value')!), '未到该时段'); assert.equal(ui.find('usage-point-exact'), undefined)
    bars[4]!.props.onClick(); await vue.nextTick(); assert.equal(textOf(ui.find('usage-point-value')!), '未记录'); assert.equal(ui.find('usage-point-exact'), undefined)
  } finally { ui.unmount() }
})

test('切换三指标保留固定日期，刷新保选择，新范围清选择并保7/31/12桶', async () => {
  const ui = mount()
  try {
    ui.bars()[0]!.props.onClick(); await vue.nextTick()
    for (const [metric, label, exact] of [['upload_bytes', '原始上传', '123,456,789 字节'], ['download_bytes', '原始下载', '10,000,000,000 字节'], ['charged_bytes', '套餐消耗', '12,345,678,901 字节']]) {
      ui.nodes().find(n => n.props['data-metric-control'])!.props.onChange(metric); await vue.nextTick()
      assert.equal(textOf(ui.find('usage-point-metric')!), label); assert.equal(textOf(ui.find('usage-point-exact')!), exact)
      assert.equal(ui.bars()[0]!.props['aria-pressed'], true)
    }
    ui.state.loading = true; await vue.nextTick(); assert.equal(ui.bars().length, 7); assert.ok(ui.find('usage-point-card')); assert.equal(ui.find('usage-timeline-loading'), undefined)
    for (const [period, count] of [['month', 31], ['year', 12]] as const) {
      ui.state.period = period; ui.state.data = fixture(period); await vue.nextTick()
      assert.equal(ui.bars().length, count); assert.equal(ui.find('usage-point-card'), undefined)
    }
    assert.match(ui.text(), /查看线路使用明细/)
    assert.doesNotMatch(ui.text(), /部分时段缺少记录|尚不能精确分配|待核对后计入/)
  } finally { ui.unmount() }
})

test('首次及跨范围加载不能画假未记录或旧范围金额，加载完成才画真实日历', async () => {
  const ui = mount(null, '7d', true)
  try {
    assert.ok(ui.find('usage-timeline-loading')); assert.equal(ui.bars().length, 0); assert.doesNotMatch(ui.text(), /未记录|0 GB/)
    ui.state.data = fixture(); ui.state.loading = false; await vue.nextTick(); assert.equal(ui.bars().length, 7)
    ui.state.period = 'month'; ui.state.loading = true; await vue.nextTick()
    assert.ok(ui.find('usage-timeline-loading')); assert.equal(ui.bars().length, 0); assert.doesNotMatch(ui.text(), /未记录|12.34 GB/)
    ui.state.data = fixture('month'); ui.state.loading = false; await vue.nextTick(); assert.equal(ui.bars().length, 31)
  } finally { ui.unmount() }
})
