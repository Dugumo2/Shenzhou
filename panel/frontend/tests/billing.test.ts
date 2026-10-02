import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as pending from '../src/billingPending.ts'
import * as api from '../src/api.ts'
import * as display from '../src/display.ts'

const body = () => ({ next_reset_at: '2028-11-16T09:30', expected_billing_revision: 1, preview_token: 'fake-preview', idempotency_key: crypto.randomUUID() })
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (reason: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); await vue.nextTick() }

test('超时后的相同请求可重试；迟到的原结果不清除待确认记录', async () => {
  const key = pending.pendingKey('admin', crypto.randomUUID()), value = pending.rememberPending(key, body())
  const late = deferred<string>()
  await assert.rejects(pending.submitPending(key, value, () => pending.boundedRequest(late.promise, 5)), { code: 'REQUEST_TIMEOUT' })
  late.resolve('too-late'); await flush()
  assert.equal(pending.getPending(key)?.idempotency_key, value.idempotency_key)
  assert.equal(await pending.submitPending(key, value, async () => 'saved'), 'saved')
  assert.equal(pending.getPending(key), null)
})

test('鉴权、忙碌、网络和非JSON失败保留请求；明确业务拒绝才释放', async () => {
  for (const [status, code] of [[401,'UNAUTHENTICATED'],[403,'FORBIDDEN'],[404,'NOT_FOUND'],[409,'BILLING_BUSY'],[409,'INVALID_RESPONSE'],[503,'INVALID_RESPONSE'],[0,'NETWORK_ERROR']] as const) {
    const key = pending.pendingKey('admin', crypto.randomUUID()), value = pending.rememberPending(key, body())
    await assert.rejects(pending.submitPending(key, value, async () => { throw new api.ApiError(status, code, '失败') }))
    assert.equal(pending.getPending(key), value)
    await pending.submitPending(key, value, async () => null)
  }
  for (const [status, code] of [[409,'BILLING_CONFLICT'],[409,'PREVIEW_INVALID'],[422,'INVALID_INPUT']] as const) {
    const key = pending.pendingKey('admin', crypto.randomUUID()), value = pending.rememberPending(key, body())
    await assert.rejects(pending.submitPending(key, value, async () => { throw new api.ApiError(status, code, '拒绝') }))
    assert.equal(pending.getPending(key), null)
  }
})

test('不同管理员和服务不混用；同请求在途重试合并', async () => {
  const id = crypto.randomUUID(), key = pending.pendingKey('admin-a',id), other = pending.pendingKey('admin-b',id)
  const value = pending.rememberPending(key, body()), d = deferred<string>(); let calls = 0
  assert.equal(pending.getPending(other), null)
  assert.equal(pending.rememberPending(key, body()), value)
  const first = pending.submitPending(key,value,()=>{ calls++; return d.promise })
  const second = pending.submitPending(key,value,async()=>{ calls++; return 'wrong' })
  await flush(); assert.equal(calls,1); d.resolve('ok')
  assert.deepEqual(await Promise.all([first,second]),['ok','ok'])
})

// 编译真实SFC的setup；仅替换网络与生命周期宿主，不复制组件的实现。
const descriptor = parse(readFileSync(new URL('../src/components/BillingDrawer.vue', import.meta.url),'utf8')).descriptor
const compiled = compileScript(descriptor, { id: 'billing-real-component' })
const js = ts.transpileModule(compiled.content, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const require = createRequire(import.meta.url)
function mount(send: (path: string, method?: string, value?: unknown) => Promise<unknown>, username = 'admin-' + crypto.randomUUID(), id = crypto.randomUUID()) {
  const hooks: Array<()=>void> = [], events: string[] = [], scope = vue.effectScope()
  const auth = vue.reactive({ session: { authenticated:true, user:{username,is_staff:true} } })
  const props = vue.reactive({service:{id}})
  const exports: Record<string,any> = {}
  const moduleRequire = (name: string) => name === 'vue' ? {...vue,onUnmounted:(fn:()=>void)=>hooks.push(fn)} : name === '../api' ? {...api,request:send} : name === '../auth' ? {auth} : name === '../display' ? display : name === '../billingPending' ? pending : require(name)
  new Function('require','exports',js)(moduleRequire,exports)
  const state = scope.run(()=>exports.default.setup(props,{expose:()=>{},emit:(name:string)=>events.push(name)}))
  return {state,props,auth,events,id,username,unmount(){hooks.forEach(fn=>fn());scope.stop()}}
}
function plan() { return {billing_revision:1,can_modify:true,plan:{next_reset_at:'2028-11-15T01:30:00Z'},used_bytes:'30000000000'} }
function preview() {return {...plan(),preview_token:'fake-preview',preview_expires_at:'2099-01-01T00:00:00Z'}}

test('真实抽屉首次非空服务会读取；保存失败跨卸载仍重试原body', async () => {
  const bodies: unknown[] = []; let fail = true
  const send = async (_:string,method='GET',value?:unknown) => {
    if(method==='GET')return plan()
    if(method==='POST')return preview()
    bodies.push(value)
    if(fail)throw new api.ApiError(503,'INVALID_RESPONSE','保存结果未知')
    return {message:'已保存',billing:plan()}
  }
  const first=mount(send); await flush(); assert.equal(first.state.planReady.value,true)
  first.state.input.value='2028-11-16T09:30'; await first.state.createPreview(); await first.state.save()
  assert.ok(first.state.requestBody.value); await first.state.refreshPlan(); assert.match(first.state.error.value,/先重试/)
  first.unmount()
  const second=mount(send,first.username,first.id); await flush()
  assert.equal(second.state.input.value,'2028-11-16T09:30'); assert.equal(second.state.canSave.value,true)
  fail=false; await second.state.save(); assert.deepEqual(bodies[0],bodies[1]); assert.equal(second.state.requestBody.value,null)
  second.unmount()
})

test('真实抽屉409刷新失败保留输入且禁止用旧修订预览', async () => {
  let reads=0,previews=0
  const ui=mount(async (_,method='GET')=>{
    if(method==='GET'){ if(++reads>1)throw new api.ApiError(0,'NETWORK_ERROR','断网');return plan() }
    previews++; throw new api.ApiError(409,'BILLING_CONFLICT','版本改变')
  })
  await flush(); ui.state.input.value='2028-11-16T09:30';await ui.state.createPreview()
  assert.equal(ui.state.input.value,'2028-11-16T09:30'); assert.equal(ui.state.planReady.value,false)
  assert.match(ui.state.error.value,/刷新失败/);await ui.state.createPreview();assert.equal(previews,1)
  ui.unmount()
})

test('真实抽屉卸载后迟到保存不向新服务显示成功', async () => {
  const d=deferred<unknown>()
  const ui=mount(async (_,method='GET')=>method==='GET'?plan():method==='POST'?preview():d.promise)
  await flush(); ui.state.input.value='2028-11-16T09:30'; await ui.state.createPreview()
  const saving=ui.state.save(); await flush();ui.unmount()
  d.resolve({message:'已保存',billing:plan()});await saving
  assert.deepEqual(ui.events,[])
  assert.equal(pending.getPending(pending.pendingKey(ui.username,ui.id)),null)
})
