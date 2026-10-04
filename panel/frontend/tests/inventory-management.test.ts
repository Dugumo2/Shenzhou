import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import { effectiveFreshness } from '../src/inventoryStatus.ts'

const source = parse(readFileSync(new URL('../src/components/InventoryMetadataEditor.vue', import.meta.url), 'utf8')).descriptor
const script = ts.transpileModule(compileScript(source, { id:'inventory-editor' }).content, { compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022} }).outputText
type Item={id:string;name:string;notes:string;revision:number}
const item:Item={id:'server-one',name:'原机器',notes:'旧备注',revision:1}
const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();await vue.nextTick()}
function deferred<T>(){let resolve!:(x:T)=>void;const promise=new Promise<T>(r=>{resolve=r});return{promise,resolve}}
function mount(send:(path:string,method?:string,body?:any)=>Promise<any>){
  const props=vue.reactive({item:{...item},kind:'servers',enabled:true}),events:Item[]=[],ends:Array<()=>void>=[]
  const scope=vue.effectScope(),exports:any={}
  const load=(name:string)=>name==='vue'?{...vue,onUnmounted:(fn:()=>void)=>ends.push(fn)}:name==='../api'?{...api,request:send}:undefined
  new Function('require','exports',script)(load,exports)
  const state=scope.run(()=>exports.default.setup(props,{expose:()=>{},emit:(name:string,row:Item)=>{if(name==='saved')events.push(row)}}))
  return{state,props,events,unmount(){ends.forEach(fn=>fn());scope.stop()}}
}

test('资料编辑发独立PATCH并回显保存结果，不夹带启停或运行参数',async()=>{
  const calls:any[]=[]
  const ui=mount(async(path,method,body)=>{calls.push({path,method,body});return{item:{...item,name:body.name,notes:body.notes,revision:2},replayed:false,message:'资料已保存'}})
  ui.state.name.value='新名字';ui.state.notes.value='新备注';await ui.state.save()
  assert.equal(calls[0].method,'PATCH');assert.equal(calls[0].path,'/admin/servers/server-one')
  assert.deepEqual(Object.keys(calls[0].body).sort(),['idempotency_key','name','notes','revision'])
  assert.equal(ui.events[0].name,'新名字');assert.equal(ui.state.revision.value,2);assert.equal(ui.state.pending.value,null)
  ui.unmount()
})

test('请求结果不确定时保留相同payload/key重试；重放后读取当前资料',async()=>{
  const writes:any[]=[];let reads=0
  const ui=mount(async(path,method,body)=>{
    if(method!=='PATCH'){reads++;return{server:{...item,name:'其他人后续修改',revision:3}}}
    writes.push({...body});if(writes.length===1)throw new api.ApiError(0,'NETWORK_ERROR','网络中断')
    return{item:{...item,name:'我保存的名字',revision:2},replayed:true,message:'原请求回执'}
  })
  ui.state.name.value='我保存的名字';await ui.state.save();assert.ok(ui.state.pending.value)
  await ui.state.save();assert.deepEqual(writes[0],writes[1]);assert.equal(reads,1)
  assert.equal(ui.events[0].revision,3);assert.equal(ui.state.name.value,'其他人后续修改');ui.unmount()
})

test('修订冲突必须读取并显式确认最新版本，原输入保留',async()=>{
  const writes:any[]=[]
  const ui=mount(async(path,method,body)=>{
    if(method!=='PATCH')return{server:{...item,name:'当前名字',revision:2}}
    writes.push(body);if(writes.length===1)throw new api.ApiError(409,'revision_conflict','资料已变化')
    return{item:{...item,name:body.name,revision:3},replayed:false,message:'保存成功'}
  })
  ui.state.name.value='我的输入';await ui.state.save();assert.equal(ui.state.conflict.value,true)
  await ui.state.save();assert.equal(writes.length,1)
  await ui.state.readLatest();assert.equal(ui.state.name.value,'我的输入');assert.equal(ui.state.latest.value.revision,2)
  ui.state.adoptLatest();await ui.state.save();assert.equal(writes[1].revision,2)
  assert.equal(writes[1].name,'我的输入');assert.notEqual(writes[0].idempotency_key,writes[1].idempotency_key);ui.unmount()
})

test('切换目标或卸载后旧保存结果不能串到新资源',async()=>{
  const pending=deferred<any>();const ui=mount(async()=>pending.promise)
  const saving=ui.state.save();ui.props.item={...item,id:'server-two',name:'第二台'};await flush()
  pending.resolve({item:{...item,name:'旧返回',revision:2},message:'已保存'});await saving
  assert.equal(ui.state.name.value,'第二台');assert.equal(ui.events.length,0);ui.unmount()
})

test('资料写入关闭或明确403时不伪报成功',async()=>{
  let count=0;const ui=mount(async()=>{count++;throw new api.ApiError(403,'permission_denied','禁止修改')})
  ui.props.enabled=false;await ui.state.save();assert.equal(count,0)
  ui.props.enabled=true;await ui.state.save();assert.equal(ui.state.notice.value,'');assert.equal(ui.events.length,0)
  assert.equal(ui.state.error.value,'禁止修改');assert.equal(ui.state.pending.value,null);ui.unmount()
})

test('已加载证据到期后只改变时效，不伪造新采样',()=>{
  const time=Date.parse('2026-10-04T00:00:00Z'),expires='2026-10-04T00:01:00Z'
  assert.equal(effectiveFreshness('fresh',expires,time),'fresh')
  assert.equal(effectiveFreshness('fresh',expires,time+60000),'stale')
  assert.equal(effectiveFreshness('target_changed',expires,time),'target_changed')
  assert.equal(effectiveFreshness('fresh',undefined,time),'unknown')
  assert.equal(effectiveFreshness('not_connected',undefined,time),'not_connected')
})
