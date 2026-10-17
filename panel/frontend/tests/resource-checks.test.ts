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
function mount(send:(path:string,method?:string,body?:any,signal?:AbortSignal)=>Promise<any>){
  const props=vue.reactive({checks:{items:[],truncated:false},actions:[{...action}]}),ends:Array<()=>void>=[],events:string[]=[]
  const exports:any={},scope=vue.effectScope()
  const load=(name:string)=>name==='vue'?{...vue,onBeforeUnmount:(fn:()=>void)=>ends.push(fn)}:name==='../api'?{...api,request:send}:name==='../display'?{formatDate:(s:any)=>s}:name==='../inventoryStatus'?{effectiveFreshness:(s:any)=>s,useInventoryClock:()=>vue.ref(Date.now())}:undefined
  new Function('require','exports',script)(load,exports)
  const state=scope.run(()=>exports.default.setup(props,{expose:()=>{},emit:(event:string)=>events.push(event)}))
  return{state,props,events,unmount(){ends.forEach(fn=>fn());scope.stop()}}
}

function fakeClock() {
  const originalSet=globalThis.setTimeout,originalClear=globalThis.clearTimeout
  const scheduled=new Map<number,{at:number;run:()=>void}>()
  let next=0,now=0
  globalThis.setTimeout=((run:()=>void,delay=0)=>{const id=++next;scheduled.set(id,{at:now+delay,run});return id}) as any
  globalThis.clearTimeout=((id:number)=>{scheduled.delete(id)}) as any
  return {
    async tick(ms:number){
      const end=now+ms
      while(true){
        const due=[...scheduled].filter(([,timer])=>timer.at<=end).sort((a,b)=>a[1].at-b[1].at)[0]
        if(!due)break
        now=due[1].at;scheduled.delete(due[0]);due[1].run();await flush()
      }
      now=end;await flush()
    },
    count:()=>scheduled.size,
    restore(){globalThis.setTimeout=originalSet;globalThis.clearTimeout=originalClear},
  }
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

test('POST已入队但响应丢失且忽略abort时15秒退出，同标识手动恢复且忽略迟到回执',async()=>{
  const clock=fakeClock(),first=deferred(),second=deferred(),calls:any[]=[]
  const ui=mount(async(path,method,body,signal)=>{calls.push({path,method,body:{...body},signal});return calls.length===1?first.promise:second.promise})
  try {
    const waiting=ui.state.start(action)
    await clock.tick(14_999);assert.equal(ui.state.busy.value,true)
    await clock.tick(1);await waiting
    assert.equal(ui.state.busy.value,false);assert.equal(calls[0].signal.aborted,true)
    assert.match(ui.state.message.value,/15秒.*结果未知/);assert.ok(ui.state.pending.value)
    assert.equal(ui.state.job.value,null);assert.deepEqual(ui.events,[])
    await clock.tick(60_000);assert.equal(calls.length,1)
    const retry=ui.state.start(action)
    assert.deepEqual(calls[0].body,calls[1].body)
    first.resolve({...finished,id:'late-old-job'});await flush()
    assert.equal(ui.state.busy.value,true);assert.equal(ui.state.job.value,null);assert.deepEqual(ui.events,[])
    second.resolve(finished);await retry
    assert.equal(ui.state.job.value.id,'job-1');assert.equal(ui.state.pending.value,null)
    assert.deepEqual(ui.events,['completed']);assert.equal(clock.count(),0)
  } finally {ui.unmount();clock.restore()}
})

test('GET悬挂超时保任务，不自动继续轮询，手动恢复后旧回执不能覆盖新结果',async()=>{
  const clock=fakeClock(),late=deferred(),calls:any[]=[]
  const queued={...finished,state:'queued',result:null,lease_until:null}
  const ui=mount(async(path,method,_body,signal)=>{
    calls.push({path,method,signal});return calls.length===1?queued:calls.length===2?late.promise:{...finished,id:'job-1'}
  })
  try {
    await ui.state.start(action)
    await clock.tick(1500);assert.equal(calls[1].method,'GET')
    await clock.tick(15_000)
    assert.equal(ui.state.busy.value,false);assert.equal(calls[1].signal.aborted,true)
    assert.equal(ui.state.job.value.id,'job-1');assert.equal(ui.state.job.value.state,'queued')
    assert.match(ui.state.message.value,/15秒.*任务已保留.*结果未知/);assert.deepEqual(ui.events,[])
    await clock.tick(60_000);assert.equal(calls.length,2)
    await ui.state.readJob();late.resolve({...queued,state:'running'});await flush()
    assert.equal(ui.state.job.value.state,'completed');assert.deepEqual(ui.events,['completed'])
    assert.deepEqual(calls.map(c=>c.method),['POST','GET','GET']);assert.equal(clock.count(),0)
  } finally {ui.unmount();clock.restore()}
})

for (const change of ['目标版本','权限目录','卸载'] as const) {
  test(`${change}变化会取消忽略abort的悬挂请求，旧响应与截止回调不能污染状态`,async()=>{
    const clock=fakeClock(),late=deferred(),signals:AbortSignal[]=[]
    const ui=mount(async(_path,_method,_body,signal)=>{signals.push(signal!);return late.promise})
    try {
      const waiting=ui.state.start(action)
      if(change==='目标版本')ui.props.actions=[{...action,target_version:'b'.repeat(64)}]
      else if(change==='权限目录')ui.props.actions=[]
      else ui.unmount()
      await flush();await waiting
      assert.equal(signals[0].aborted,true);assert.equal(ui.state.busy.value,false)
      assert.equal(ui.state.pending.value,null);assert.equal(ui.state.job.value,null)
      late.resolve(finished);await clock.tick(60_000)
      assert.equal(ui.state.message.value,'');assert.deepEqual(ui.events,[]);assert.equal(signals.length,1)
      assert.equal(clock.count(),0)
    } finally {if(change!=='卸载')ui.unmount();clock.restore()}
  })
}

for (const method of ['POST','GET']) {
  for (const status of [401,403]) {
    test(`${method}返回${status}停止旧任务和动作，不以旧目录继续恢复`,async()=>{
      const clock=fakeClock(),calls:string[]=[]
      const ui=mount(async(_path,requestMethod)=>{
        calls.push(requestMethod!)
        if(requestMethod===method)throw new api.ApiError(status,'permission_denied','管理员权限已失效')
        return {...finished,state:'queued',result:null,lease_until:null}
      })
      try {
        await ui.state.start(action)
        if(method==='GET')await ui.state.readJob()
        assert.equal(ui.state.busy.value,false);assert.equal(ui.state.permissionDenied.value,true)
        assert.equal(ui.state.pending.value,null);assert.equal(ui.state.job.value,null)
        const count=calls.length
        await ui.state.start(action);await ui.state.readJob();await clock.tick(60_000)
        assert.equal(calls.length,count);assert.match(ui.state.message.value,/权限已失效/)
        assert.deepEqual(ui.events,[]);assert.equal(clock.count(),0)
      } finally {ui.unmount();clock.restore()}
    })
  }
}

test('目标版本切换后旧请求不会解除新请求等待或覆盖新回执',async()=>{
  const clock=fakeClock(),first=deferred(),second=deferred();let count=0
  const ui=mount(async()=>++count===1?first.promise:second.promise)
  try {
    const oldRequest=ui.state.start(action)
    const nextAction={...action,target_version:'b'.repeat(64)}
    ui.props.actions=[nextAction]
    const newRequest=ui.state.start(nextAction)
    await oldRequest;assert.equal(ui.state.busy.value,true)
    first.resolve(finished);await flush();assert.equal(ui.state.job.value,null)
    second.resolve({...finished,target_version:nextAction.target_version});await newRequest
    assert.equal(ui.state.job.value.target_version,nextAction.target_version)
    assert.deepEqual(ui.events,['completed']);assert.equal(clock.count(),0)
  } finally {ui.unmount();clock.restore()}
})
