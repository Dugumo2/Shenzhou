import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'

type Item = { id: string; name: string; notes?: string; revision: number }
type Host = { tag: string; text: string; props: Record<string, any>; children: Host[]; parent: Host | null }
const node = (tag: string, text = ''): Host => ({ tag, text, props: {}, children: [], parent: null })
const walk = (item: Host): Host[] => [item, ...item.children.flatMap(walk)]
const textOf = (item: Host): string => item.text + item.children.map(textOf).join('')
const flush = async () => { for (let i = 0; i < 16; i++) await Promise.resolve(); await vue.nextTick() }
const original: Item = { id: 'core-one', name: '原核心', notes: '原备注', revision: 1 }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }

// 编译真实模板并操作按钮和输入事件；此宿主不代替浏览器布局验收。
function mount(send: (path: string, method?: string, body?: any) => Promise<any>, kind = 'cores', item = original, list = false) {
  function loadComponent(file: string): any {
    const source = readFileSync(new URL('../src/components/' + file, import.meta.url), 'utf8')
    const script = compileScript(parse(source).descriptor, { id: 'resource-metadata-editor', inlineTemplate: true })
    const code = ts.transpileModule(script.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
    const exports: Record<string, any> = {}
    new Function('require', 'exports', code)((name: string) => name === 'vue' ? vue : name === '../api' ? { ...api, request: send }
      : name === './ResourceMetadataEditor.vue' ? loadComponent('ResourceMetadataEditor.vue')
        // 原详情的完整渲染由 node-resources.test.ts 验证，此处隔离表单到列表的事件接线。
        : name === './NodeResourceDetails.vue' ? { default: { render: () => vue.h('section', '原节点详情') } } : undefined, exports)
    return exports
  }
  const component = loadComponent(list ? 'NodeResourceList.vue' : 'ResourceMetadataEditor.vue').default
  const props = vue.reactive({ item: { ...item }, kind, enabled: true, metadataEditable: true,
    nodes: [{ id: 'egress:' + item.id, resource_id: item.id, resource_type: 'egress', name: item.name, revision: item.revision, kind: 'external', protocol_label: 'Shadowsocks 2022' }] })
  const events: Item[] = []
  const renderer = vue.createRenderer<Host, Host>({
    createElement: tag => node(tag), createText: text => node('#text', text), createComment: text => node('#comment', text),
    insert(child, parent, anchor) { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = parent; const index = anchor ? parent.children.indexOf(anchor) : -1; if (index < 0) parent.children.push(child); else parent.children.splice(index, 0, child) },
    remove(child) { if (child.parent) child.parent.children.splice(child.parent.children.indexOf(child), 1); child.parent = null },
    setText(item, text) { item.text = text }, setElementText(item, text) { item.text = text; item.children = [] },
    parentNode: item => item.parent, nextSibling: item => item.parent?.children[item.parent.children.indexOf(item) + 1] || null,
    patchProp(item, key, _old, value) { item.props[key] = value },
  })
  const app = renderer.createApp({ setup: () => () => vue.h(component, { ...(list ? { nodes: props.nodes, metadataEditable: props.metadataEditable } : { item: props.item, kind: props.kind, enabled: props.enabled }), onSaved: (row: Item) => events.push(row) }) })
  app.component('ElButton', { setup: (_: any, { attrs, slots }: any) => () => vue.h('button', attrs, slots.default?.()) })
  app.component('ElForm', { setup: (_: any, { attrs, slots }: any) => () => vue.h('form', attrs, slots.default?.()) })
  app.component('ElFormItem', { props: ['label'], setup: (p: any, { slots }: any) => () => vue.h('label', [p.label, slots.default?.()]) })
  app.component('ElInput', { props: ['modelValue'], emits: ['update:modelValue'], setup: (p: any, { attrs, emit }: any) => () => vue.h('input', { ...attrs, value: p.modelValue, onInput: (value: string) => emit('update:modelValue', value) }) })
  app.component('ElAlert', { props: ['title'], setup: (p: any) => () => vue.h('p', { role: 'alert' }, p.title) })
  const host = node('root'); app.mount(host)
  const nodes = () => walk(host)
  return { props, events, nodes, text: () => textOf(host),
    input: (label: string) => nodes().find(n => n.tag === 'input' && n.props['aria-label'] === label),
    button: (label: string) => nodes().find(n => n.tag === 'button' && textOf(n) === label),
    async click(label: string) { const button = nodes().find(n => n.tag === 'button' && textOf(n) === label); assert.ok(button, label); assert.ok(!button.props.disabled, label + '不可点击'); await button.props.onClick(); await flush() },
    async fill(label: string, value: string) { const field = nodes().find(n => n.tag === 'input' && n.props['aria-label'] === label); assert.ok(field, label); field.props.onInput(value); await flush() },
    unmount: () => app.unmount(),
  }
}

const read = (item = original, enabled = true) => ({ item, read_only: !enabled, capabilities: { edit: enabled } })
const receipt = (item = original, replayed = false) => ({ item, replayed, message: '资料已保存。' })
async function prepare(ui: ReturnType<typeof mount>, name = '我的新名称') {
  await ui.click('编辑资料'); await ui.fill('资源别名', name); await ui.click('预览修改')
}

test('核心先读当前资料，预览不写入，确认只保存名称备注并以真实回执报成功', async () => {
  const calls: any[] = []
  const ui = mount(async (path, method, body) => { calls.push({ path, method, body }); return method === 'PATCH' ? receipt({ ...original, ...body, revision: 2 }) : read() })
  try {
    assert.equal(ui.input('资源别名'), undefined)
    await ui.click('编辑资料'); await ui.fill('资源别名', '新核心'); await ui.fill('资源备注', '新的登记备注'); await ui.click('预览修改')
    assert.equal(calls.length, 1); assert.match(ui.text(), /原核心.*新核心/); assert.match(ui.text(), /原备注.*新的登记备注/)
    await ui.click('确认保存资料')
    assert.equal(calls[1].path, '/admin/cores/core-one'); assert.equal(calls[1].method, 'PATCH')
    assert.deepEqual(Object.keys(calls[1].body).sort(), ['idempotency_key', 'name', 'notes', 'revision'])
    assert.equal(calls[1].body.revision, 1); assert.equal(ui.events[0]!.revision, 2)
    assert.match(ui.text(), /资料已保存/); assert.doesNotMatch(ui.text(), /运行已应用|配置已生效/)
  } finally { ui.unmount() }
})

test('接入与出站节点用真实ID读取资料，只发名称且不显示备注输入', async () => {
  for (const kind of ['ingresses', 'egresses']) {
    const item = { id: '17', name: '实际节点名称', revision: 8 }; const calls: any[] = []
    const ui = mount(async (path, method, body) => { calls.push({ path, method, body }); return method === 'PATCH' ? receipt({ ...item, name: body.name, revision: 9 }) : read(item) }, kind, { ...item, name: '名称待核实' })
    try {
      await ui.click('编辑资料'); assert.equal(ui.input('资源别名')!.props.value, item.name); assert.equal(ui.input('资源备注'), undefined)
      await ui.fill('资源别名', '改后的节点'); await ui.click('预览修改'); await ui.click('确认保存资料')
      assert.equal(calls[0].path, '/admin/' + kind + '/17'); assert.equal(calls[1].path, calls[0].path)
      assert.deepEqual(Object.keys(calls[1].body).sort(), ['idempotency_key', 'name', 'revision']); assert.equal(calls[1].body.revision, 8)
    } finally { ui.unmount() }
  }
})

test('网络未知、写入竞争、服务端失败均保留同一payload和幂等键重试', async () => {
  for (const failure of [new api.ApiError(0, 'NETWORK_ERROR', '网络中断'), new api.ApiError(409, 'write_conflict', '正在保存'), new api.ApiError(500, 'server_error', '服务错误'), new api.ApiError(403, 'INVALID_RESPONSE', '回执无效')]) {
    const writes: any[] = []; const ui = mount(async (_path, method, body) => {
      if (method !== 'PATCH') return read()
      writes.push({ ...body }); if (writes.length === 1) throw failure
      return receipt({ ...original, name: body.name, revision: 2 })
    })
    try {
      await prepare(ui); await ui.click('确认保存资料'); assert.equal(ui.events.length, 0)
      assert.equal(ui.input('资源别名')!.props.value, '我的新名称'); assert.match(ui.text(), /保存结果尚未确认/)
      assert.equal(ui.nodes().find(n => n.tag === 'form')!.props.disabled, true)
      await ui.click('重试原保存请求'); assert.deepEqual(writes[0], writes[1]); assert.equal(ui.events.length, 1)
    } finally { ui.unmount() }
  }
})

test('409冲突保输入，显式读取和采用版本后必须重新预览且使用新幂等键', async () => {
  const writes: any[] = []; let reads = 0
  const ui = mount(async (_path, method, body) => {
    if (method !== 'PATCH') return read(++reads === 1 ? original : { ...original, name: '别人保存的名称', notes: '别人的备注', revision: 2 })
    writes.push({ ...body }); if (writes.length === 1) throw new api.ApiError(409, 'revision_conflict', '资料已变化')
    return receipt({ ...original, ...body, revision: 3 })
  })
  try {
    await prepare(ui); await ui.click('确认保存资料'); assert.equal(ui.button('预览修改')!.props.disabled, true)
    await ui.click('读取当前版本，保留输入'); assert.equal(ui.input('资源别名')!.props.value, '我的新名称'); assert.match(ui.text(), /别人保存的名称/)
    await ui.click('核对后采用最新版本，保留我的输入'); assert.equal(ui.input('资源别名')!.props.value, '我的新名称')
    assert.equal(ui.button('确认保存资料'), undefined); await ui.click('预览修改'); assert.match(ui.text(), /别人保存的名称.*我的新名称/)
    await ui.click('确认保存资料'); assert.equal(writes[1].revision, 2); assert.notEqual(writes[0].idempotency_key, writes[1].idempotency_key)
  } finally { ui.unmount() }
})

test('重复回执必须读取当前版本，读取失败不丢原请求或虚报成功', async () => {
  const writes: any[] = []; let reads = 0
  const ui = mount(async (_path, method, body) => {
    if (method === 'PATCH') { writes.push({ ...body }); return receipt({ ...original, name: body.name, revision: 2 }, true) }
    reads++; if (reads === 2) throw new api.ApiError(403, 'permission_denied', '暂不可读取')
    return read(reads === 1 ? original : { ...original, name: '后来修改的名称', revision: 3 })
  })
  try {
    await prepare(ui); await ui.click('确认保存资料'); assert.equal(ui.events.length, 0); assert.doesNotMatch(ui.text(), /资料已保存/)
    await ui.click('重试原保存请求'); assert.deepEqual(writes[0], writes[1]); assert.equal(ui.input('资源别名')!.props.value, '后来修改的名称'); assert.equal(ui.events[0]!.revision, 3)
  } finally { ui.unmount() }
})

test('刷新相同对象保输入和预览，切换同ID不同类型隔离迟到保存', async () => {
  const pending = deferred<any>(); const writes: any[] = []
  const ui = mount(async (_path, method, body) => { if (method !== 'PATCH') return read(); writes.push(body); return pending.promise })
  try {
    await prepare(ui); ui.props.item = { ...original, name: '后台刷新名称', revision: 9 }; await flush()
    assert.equal(ui.input('资源别名')!.props.value, '我的新名称'); assert.ok(ui.button('确认保存资料'))
    const saving = ui.button('确认保存资料')!.props.onClick(); await flush(); assert.equal(writes.length, 1)
    ui.props.kind = 'egresses'; await flush(); pending.resolve(receipt({ ...original, name: '旧返回', revision: 2 })); await saving; await flush()
    assert.equal(ui.events.length, 0); assert.equal(ui.input('资源别名'), undefined); assert.ok(ui.button('编辑资料'))
  } finally { ui.unmount() }
})

test('初次读取失败保原详情，切换目标和卸载后旧读取不污染编辑器', async () => {
  const pending = deferred<any>(); const ui = mount(async () => pending.promise)
  try {
    const reading = ui.button('编辑资料')!.props.onClick(); await flush(); assert.equal(ui.input('资源别名'), undefined)
    ui.props.item = { ...original, id: 'core-two', name: '第二核心' }; await flush(); pending.resolve(read()); await reading; await flush()
    assert.equal(ui.input('资源别名'), undefined); assert.equal(ui.events.length, 0); assert.doesNotMatch(ui.text(), /原核心/)
  } finally { ui.unmount() }
  const late = deferred<any>(); const unmounted = mount(async () => late.promise)
  const reading = unmounted.button('编辑资料')!.props.onClick(); unmounted.unmount(); late.resolve(read()); await reading
  assert.equal(unmounted.events.length, 0)
})

test('写权限关闭和服务端拒绝不伪报成功，输入可保留并修正', async () => {
  let calls = 0
  const ui = mount(async (_path, method) => { calls++; if (method === 'PATCH') throw new api.ApiError(403, 'metadata_read_only', '当前环境未开放资料修改'); return read() })
  try {
    ui.props.enabled = false; await flush(); assert.equal(ui.button('编辑资料')!.props.disabled, true)
    await ui.button('编辑资料')!.props.onClick(); assert.equal(calls, 0)
    ui.props.enabled = true; await flush(); await prepare(ui); await ui.click('确认保存资料')
    assert.equal(ui.events.length, 0); assert.equal(ui.input('资源别名')!.props.value, '我的新名称'); assert.equal(ui.button('重试原保存请求'), undefined); assert.doesNotMatch(ui.text(), /资料已保存/)
  } finally { ui.unmount() }
  const readOnly = mount(async () => read(original, false))
  try { await readOnly.click('编辑资料'); await readOnly.fill('资源别名', '新名称'); assert.equal(readOnly.button('预览修改')!.props.disabled, true); assert.match(readOnly.text(), /此环境未开放资料修改/) } finally { readOnly.unmount() }
})

test('不接受空名称、超长资料、无改动或错误目标的成功回执', async () => {
  const writes: any[] = []; const ui = mount(async (_path, method, body) => { if (method !== 'PATCH') return read(); writes.push(body); return receipt({ ...original, id: 'wrong-target' }) })
  try {
    await ui.click('编辑资料'); assert.equal(ui.button('预览修改')!.props.disabled, true)
    for (const value of ['', '   ', '名'.repeat(101)]) { await ui.fill('资源别名', value); assert.equal(ui.button('预览修改')!.props.disabled, true) }
    await ui.fill('资源别名', '有效名称'); await ui.fill('资源备注', '注'.repeat(1001)); assert.equal(ui.button('预览修改')!.props.disabled, true)
    await ui.fill('资源备注', '有效备注'); await ui.click('预览修改'); await ui.click('确认保存资料')
    assert.equal(ui.events.length, 0); assert.match(ui.text(), /未能确认资料回执/); await ui.click('重试原保存请求'); assert.equal(writes[0].idempotency_key, writes[1].idempotency_key)
  } finally { ui.unmount() }
})

test('节点列表真实接线保留展开项，撤权保留未知请求，恢复后同键保存并返回前缀ID', async () => {
  const item = { id: '31', name: '外部节点', revision: 1 }; const writes: any[] = []
  const ui = mount(async (path, method, body) => {
    assert.equal(path, '/admin/egresses/31')
    if (method !== 'PATCH') return read(item)
    writes.push({ ...body }); if (writes.length === 1) throw new api.ApiError(0, 'NETWORK_ERROR', '网络中断')
    return receipt({ ...item, name: body.name, revision: 2 })
  }, 'egresses', item, true)
  try {
    const details = ui.nodes().find(n => n.tag === 'details')!; details.props.open = true
    await prepare(ui); await ui.click('确认保存资料'); ui.props.metadataEditable = false; await flush()
    assert.equal(ui.input('资源别名')!.props.value, '我的新名称'); assert.equal(ui.button('重试原保存请求')!.props.disabled, true)
    ui.props.nodes = [{ ...ui.props.nodes[0]!, name: '后台新名称', revision: 3 }]; await flush()
    assert.equal(ui.nodes().find(n => n.tag === 'details'), details); assert.equal(details.props.open, true)
    assert.match(ui.text(), /原节点详情|Shadowsocks 2022/); assert.equal(ui.input('资源别名')!.props.value, '我的新名称')
    ui.props.metadataEditable = true; await flush(); await ui.click('重试原保存请求')
    assert.deepEqual(writes[0], writes[1]); assert.equal(ui.events[0]!.id, 'egress:31')
  } finally { ui.unmount() }
})

test('初读失败可重新读取，等待时重复入口不新增请求', async () => {
  let reads = 0; const pending = deferred<any>()
  const ui = mount(async () => { reads++; if (reads === 1) throw new api.ApiError(0, 'NETWORK_ERROR', '读取暂时失败'); return pending.promise })
  try {
    await ui.click('编辑资料'); assert.equal(ui.input('资源别名'), undefined); assert.match(ui.text(), /读取暂时失败/)
    const reading = ui.button('重新读取资料')!.props.onClick(); await flush()
    assert.equal(ui.button('重新读取资料')!.props.disabled, true); await ui.button('重新读取资料')!.props.onClick(); assert.equal(reads, 2)
    pending.resolve(read()); await reading; await flush(); assert.equal(ui.input('资源别名')!.props.value, original.name)
  } finally { ui.unmount() }
})
