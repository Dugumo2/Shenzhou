import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'

const descriptor=parse(readFileSync(new URL('../src/components/InventoryChecks.vue',import.meta.url),'utf8')).descriptor
const script=ts.transpileModule(compileScript(descriptor,{id:'resource-checks'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
const action={target_kind:'ingress',target_id:'1',target_version:'a'.repeat(64),action:'tcp',label:'隔离TCP',timeout_seconds:5,min_interval_seconds:60,scope:'仅TCP'}
const finished={id:'job-1',...action,state:'completed',failure_code:'',result:{result:'pass',latency_ms:2},created_at:new Date().toISOString(),started_at:null,finished_at:null,lease_until:null}
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();await vue.nextTick()}
function deferred(){let resolve!:(value:any)=>void;const promise=new Promise<any>(r=>{resolve=r});return{promise,resolve}}
function mount(send:(path:string,method?:string,body?:any)=>Promise<any>){
  const props=vue.reactive({checks:{items:[],truncated:false},actions:[{...action}]}),ends:Array<()=>void>=[],events:string[]=[]
  const exports:any={},scope=vue.effectScope()
  const load=(name:string)=>name==='vue'?{...vue,onBeforeUnmount:(fn:()=>void)=>ends.push(fn)}:name==='../api'?{...api,request:send}:name==='../display'?{formatDate:(s:any)=>s}:name==='../inventoryStatus'?{effectiveFreshness:(s:any)=>s,useInventoryClock:()=>vue.ref(Date.now())}:undefined
  new Function('require','exports',script)(load,exports)
  const state=scope.run(()=>exports.default.setup(props,{expose:()=>{},emit:(event:string)=>events.push(event)}))
  return{state,props,events,unmount(){ends.forEach(fn=>fn());scope.stop()}}
}

test('挂载与旧记录刷新不发检查，显式按钮只提交固定五字段',async()=>{
  const calls:any[]=[];const ui=mount(async(path,method,body)=>{calls.push({path,method,body});return finished})
  await flush();ui.props.checks={items:[],truncated:true};await flush();assert.equal(calls.length,0)
  await ui.state.start(action)
  assert.equal(calls[0].method,'POST');assert.equal(calls[0].path,'/admin/resource-checks')
  assert.deepEqual(Object.keys(calls[0].body).sort(),['action','idempotency_key','target_id','target_kind','target_version'])
  assert.deepEqual(ui.events,['completed']);ui.unmount()
})

test('网络结果不确定时原请求标识重试，并阻止双击',async()=>{
  const gate=deferred(),writes:any[]=[]
  const ui=mount(async(_p,_m,body)=>{writes.push({...body});if(writes.length===1){await gate.promise;throw new api.ApiError(0,'NETWORK_ERROR','中断')}return finished})
  const running=ui.state.start(action);await ui.state.start(action);assert.equal(writes.length,1)
  gate.resolve(null);await running;assert.ok(ui.state.pending.value)
  await ui.state.start(action);assert.deepEqual(writes[0],writes[1]);ui.unmount()
})

test('切换目标后忽略迟到回执且不自动探测新目标',async()=>{
  const gate=deferred();let count=0;const ui=mount(async()=>{count++;return gate.promise})
  const running=ui.state.start(action)
  ui.props.actions=[{...action,target_id:'2'}];await flush()
  gate.resolve(finished);await running
  assert.equal(ui.state.job.value,null);assert.equal(ui.events.length,0);assert.equal(count,1);ui.unmount()
})

test('任务状态读取只发GET，失败保留任务且不声称通过',async()=>{
  const calls:any[]=[];const ui=mount(async(path,method)=>{
    calls.push({path,method});if(method==='GET')throw new api.ApiError(503,'UNAVAILABLE','后台不可用')
    return{...finished,state:'queued',lease_until:'2000-01-01T00:00:00Z'}
  })
  await ui.state.start(action);await ui.state.readJob()
  assert.equal(calls[1].method,'GET');assert.equal(ui.state.job.value.state,'queued');assert.equal(ui.events.length,0)
  assert.match(ui.state.message.value,/后台不可用/);ui.unmount()
})

test('没有批准目录时调用动作也无法提交',async()=>{
  let count=0;const ui=mount(async()=>{count++;return finished});ui.props.actions=[];await flush()
  await ui.state.start(action);assert.equal(count,0);ui.unmount()
})
