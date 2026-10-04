import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import * as pending from '../src/rulePending.ts'
import * as billing from '../src/billingPending.ts'

function deferred<T>() {
  let resolve!:(value:T)=>void, reject!:(reason:unknown)=>void
  const promise=new Promise<T>((yes,no)=>{resolve=yes;reject=no})
  return {promise,resolve,reject}
}
const flush=async()=>{for(let i=0;i<15;i++)await Promise.resolve();await vue.nextTick()}
const row=(revision=1,value='old.example.com')=>({id:1,action:'proxy',kind:'suffix',value,scope_domain:'',enabled:true,revision,validation:'valid'})
const listing=(revision=1)=>({read_only:false,items:[row(revision)],sources:[],message:'本地候选',candidate_revision:'snapshot-'+revision})
const ack=(revision=2)=>({message:'仅本地候选已保存',conflicts:[],item:row(revision),candidate_revision:'snapshot-'+revision,replayed:false})
const operation=()=>({path:'/admin/rules',method:'POST' as const,
  body:{action:'proxy',kind:'suffix',value:'example.com',scope_domain:'',enabled:true,idempotency_key:crypto.randomUUID()},
  rule:null,draft:{action:'proxy',kind:'suffix',value:'example.com',scope_domain:'',enabled:true}})

test('规则请求超时后保留原幂等键；迟到结果不能清掉待确认状态',async()=>{
  const key=pending.rulePendingKey('admin-'+crypto.randomUUID()),op=pending.rememberRulePending(key,operation())
  const late=deferred<unknown>()
  await assert.rejects(pending.submitRulePending(key,op,()=>late.promise,5),{code:'REQUEST_TIMEOUT'})
  assert.equal(pending.getRulePending(key),op)
  late.resolve(ack());await flush();assert.equal(pending.getRulePending(key),op)
  await pending.submitRulePending(key,op,async()=>ack())
  assert.equal(pending.getRulePending(key),null)
})

test('不同管理员隔离，在途同操作合并且请求快照不能改变',async()=>{
  const key=pending.rulePendingKey('a-'+crypto.randomUUID()),other=pending.rulePendingKey('b-'+crypto.randomUUID())
  const op=pending.rememberRulePending(key,operation()),late=deferred<string>();let calls=0
  assert.equal(pending.getRulePending(other),null)
  assert.equal(pending.rememberRulePending(key,operation()),op)
  assert.throws(()=>{op.body.value='unsafe-change'},TypeError)
  const first=pending.submitRulePending(key,op,()=>{calls++;return late.promise})
  const second=pending.submitRulePending(key,op,async()=>{calls++;return 'wrong'})
  await flush();assert.equal(calls,1);late.resolve('saved')
  assert.deepEqual(await Promise.all([first,second]),['saved','saved'])
})

test('鉴权、忙碌及非JSON失败不丢请求；明确规则拒绝才释放',async()=>{
  for(const [status,code] of [[401,'authentication_required'],[403,'candidate_only'],[409,'database_busy'],[409,'INVALID_RESPONSE'],[409,'REQUEST_FAILED'],[404,'INVALID_RESPONSE'],[0,'NETWORK_ERROR']] as const){
    const key=pending.rulePendingKey(crypto.randomUUID()),op=pending.rememberRulePending(key,operation())
    await assert.rejects(pending.submitRulePending(key,op,async()=>{throw new api.ApiError(status,code,'待确认')}))
    assert.equal(pending.getRulePending(key),op)
    await pending.submitRulePending(key,op,async()=>ack())
  }
  for(const [status,code] of [[409,'revision_conflict'],[409,'duplicate_rule'],[422,'invalid_rule'],[404,'not_found']] as const){
    const key=pending.rulePendingKey(crypto.randomUUID()),op=pending.rememberRulePending(key,operation())
    await assert.rejects(pending.submitRulePending(key,op,async()=>{throw new api.ApiError(status,code,'明确拒绝')}))
    assert.equal(pending.getRulePending(key),null)
  }
})

// 执行真实规则页 setup，仅替换网络和生命周期宿主。
const descriptor=parse(readFileSync(new URL('../src/pages/RulesPage.vue',import.meta.url),'utf8')).descriptor
const compiled=compileScript(descriptor,{id:'real-rules-page'})
const js=ts.transpileModule(compiled.content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
const require=createRequire(import.meta.url)
function mount(send:(path:string,method?:string,body?:any)=>Promise<unknown>,username='admin-'+crypto.randomUUID(),verify?:()=>Promise<{username:string;is_staff:boolean}|null>){
  const starts:Array<()=>Promise<void>>=[],ends:Array<()=>void>=[],scope=vue.effectScope()
  const auth=vue.reactive({ready:true,session:{authenticated:true,user:{username,is_staff:true} as {username:string;is_staff:boolean}|null}})
  const hydrateSession=async()=>{const user=verify?await verify():{username,is_staff:true};auth.session={authenticated:!!user,user};auth.ready=true}
  const exports:Record<string,any>={}
  const moduleRequire=(name:string)=>name==='vue'?{...vue,onMounted:(fn:()=>Promise<void>)=>starts.push(fn),onUnmounted:(fn:()=>void)=>ends.push(fn)}:
    name==='../api'?{...api,request:send}:name==='../auth'?{auth,hydrateSession}:name==='../billingPending'?billing:name==='../rulePending'?pending:name.endsWith('.vue')?{default:name}:require(name)
  new Function('require','exports',js)(moduleRequire,exports)
  const state=scope.run(()=>exports.default.setup({},{expose:()=>{}}))
  const ready=Promise.all(starts.map(fn=>fn())).then(flush)
  return {state,auth,username,ready,unmount(){ends.forEach(fn=>fn());scope.stop()}}
}

test('真实页面跨卸载恢复原请求；先核验会话再显示旧管理员草稿',async()=>{
  const bodies:any[]=[];let fail=true
  const send=async(_:string,method='GET',body?:unknown)=>{if(method==='GET')return listing();bodies.push(body);if(fail)throw new api.ApiError(503,'INVALID_RESPONSE','未知结果');return ack()}
  const first=mount(send);await first.ready;first.state.openEditor();first.state.form.value='draft.example.com';await first.state.save()
  const original=first.state.pending.value
  assert.ok(original);first.unmount()
  const validation=deferred<{username:string;is_staff:boolean}|null>()
  const second=mount(send,first.username,()=>validation.promise)
  await flush();assert.equal(second.state.pending.value,null);assert.equal(second.state.editOpen.value,false)
  validation.resolve({username:first.username,is_staff:true});await second.ready
  assert.equal(second.state.form.value,'draft.example.com');assert.equal(second.state.pending.value.body.idempotency_key,original.body.idempotency_key)
  fail=false;await second.state.save();assert.deepEqual(bodies[0],bodies[1]);assert.equal(second.state.pending.value,null)
  second.unmount()
})

test('核验后权限不足不会挂载旧管理员请求或读取规则',async()=>{
  const username='admin-'+crypto.randomUUID(),key=pending.rulePendingKey(username)
  const op=pending.rememberRulePending(key,operation());let calls=0
  const ui=mount(async()=>{calls++;return listing()},username,async()=>({username:'普通用户',is_staff:false}))
  await ui.ready;assert.equal(calls,0);assert.equal(ui.state.pending.value,null);assert.equal(ui.state.editOpen.value,false)
  assert.equal(pending.getRulePending(key),op);ui.unmount()
  await pending.submitRulePending(key,op,async()=>ack())
})

test('真实409读取当前版本但保留输入，必须核对采用后才能重试',async()=>{
  const bodies:any[]=[];let reads=0
  const ui=mount(async(_,method='GET',body)=>{
    if(method==='GET')return listing(++reads===1?1:2)
    bodies.push(body);if(bodies.length===1)throw new api.ApiError(409,'revision_conflict','版本变化')
    return ack(3)
  })
  await ui.ready;ui.state.openEditor(row());ui.state.form.value='preserved.example.com';await ui.state.save()
  assert.equal(ui.state.form.value,'preserved.example.com');assert.equal(ui.state.editing.value.revision,1)
  assert.equal(ui.state.latestRule.value.revision,2);assert.equal(ui.state.conflict.value,true)
  await ui.state.save();assert.equal(bodies.length,1)
  ui.state.adoptLatest();await ui.state.save()
  assert.equal(bodies[1].revision,2);assert.equal(bodies[1].value,'preserved.example.com')
  assert.notEqual(bodies[0].idempotency_key,bodies[1].idempotency_key);ui.unmount()
})

test('409刷新失败不能沿用旧版本；恢复读取后仍保留用户输入',async()=>{
  let reads=0,writes=0,fail=true
  const ui=mount(async(_,method='GET')=>{
    if(method==='GET'){if(++reads>1&&fail)throw new api.ApiError(0,'NETWORK_ERROR','断网');return listing(reads===1?1:2)}
    writes++;throw new api.ApiError(409,'revision_conflict','版本变化')
  })
  await ui.ready;ui.state.openEditor(row());ui.state.form.value='kept.example.com';await ui.state.save()
  assert.equal(ui.state.conflictReady.value,false);ui.state.adoptLatest();await ui.state.save();assert.equal(writes,1)
  fail=false;await ui.state.refreshConflict();assert.equal(ui.state.latestRule.value.revision,2)
  assert.equal(ui.state.form.value,'kept.example.com');ui.unmount()
})

test('保存使迟到预览失效并释放loading；刷新清除旧匹配快照',async()=>{
  const late=deferred<unknown>();let checks=0
  const match={domain:'example.com',result:'proxy_candidate',final_action:'proxy',matched_id:1,source_id:'custom',order_semantics:'代理候选优先',message:'仅候选',candidate_revision:'snapshot-1',related:[],conflicts:[]}
  const ui=mount(async(path,method='GET')=>method==='GET'?listing():path.endsWith('/preview')?(++checks===1?late.promise:match):ack())
  await ui.ready;ui.state.matchDomain.value='example.com';await flush()
  const inspecting=ui.state.inspect();await flush();assert.equal(ui.state.matchBusy.value,true)
  await ui.state.toggle(row());assert.equal(ui.state.matchBusy.value,false)
  late.resolve(match);await inspecting;assert.equal(ui.state.match.value,null)
  await ui.state.inspect();assert.equal(ui.state.match.value.domain,'example.com')
  await ui.state.load();assert.equal(ui.state.match.value,null);ui.unmount()
})

test('切换管理员后迟到保存不改新用户页面与请求空间',async()=>{
  const late=deferred<unknown>(),ui=mount(async(_,method='GET')=>method==='GET'?listing():late.promise)
  await ui.ready;ui.state.openEditor();ui.state.form.value='private-draft.example.com'
  const saving=ui.state.save();await flush();const oldKey=pending.rulePendingKey(ui.username)
  ui.auth.session={authenticated:true,user:{username:'other-'+crypto.randomUUID(),is_staff:true}}
  await flush();assert.equal(ui.state.pending.value,null);assert.equal(ui.state.form.value,'');assert.equal(ui.state.editOpen.value,false)
  assert.ok(pending.getRulePending(oldKey));late.resolve(ack());await saving
  assert.equal(ui.state.notice.value,'');assert.equal(pending.getRulePending(oldKey),null);ui.unmount()
})

test('JSON成功回执缺字段时仍保留原请求，不误报保存成功',async()=>{
  let fail=true;const bodies:any[]=[]
  const ui=mount(async(_,method='GET',body)=>{if(method==='GET')return listing();bodies.push(body);return fail?{}:ack()})
  await ui.ready;ui.state.openEditor();ui.state.form.value='ack.example.com';await ui.state.save()
  assert.ok(ui.state.pending.value);assert.equal(ui.state.notice.value,'');assert.match(ui.state.editError.value,/回执不完整/)
  fail=false;await ui.state.save();assert.deepEqual(bodies[0],bodies[1]);assert.equal(ui.state.pending.value,null);ui.unmount()
})
