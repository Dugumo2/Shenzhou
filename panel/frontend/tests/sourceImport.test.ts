import test from 'node:test'
import assert from 'node:assert/strict'
import { effectScope, ref } from 'vue'
import { ApiError } from '../src/api.ts'
import { useSourceImport, rememberSource, sendSource, sourcePending, type SourceSave } from '../src/sourceImport.ts'

const document=JSON.stringify({schema_version:1,rules:[{action:'proxy',kind:'suffix',value:'example.com'}]})
const body=():SourceSave=>({source_id:null,name:'来源',expected_revision:0,document,preview_token:'fake',idempotency_key:crypto.randomUUID()})
const item={source_id:'source',name:'来源',revision:1,state:'candidate_unbound',sha256:'abc',count:1,updated_at:''}
const preview=()=>({preview_token:'fake',can_commit:true,sha256:'abc',count:1,items:[],diff:{added:[],removed:[],modified:[],counts:{}},conflicts:[],message:'候选'})
const receipt=()=>({item,version:{revision:1,sha256:'abc'},replayed:false,message:'已保存候选'})
function deferred<T>(){let resolve!:(value:T)=>void;const promise=new Promise<T>(r=>{resolve=r});return {promise,resolve}}
function setup(send:(path:string,method?:string,body?:any)=>Promise<any>){
  const identity=ref<string>(crypto.randomUUID()),scope=effectScope();let calls=0
  const editor=scope.run(()=>useSourceImport(identity,async()=>{calls++},send))!
  editor.begin();editor.draft.value={...body()}
  return {identity,editor,calls:()=>calls,stop(){editor.dispose();scope.stop()}}
}
test('预览后修改任一输入清掉预览，不允许未确认保存',async()=>{
  let saves=0;const ui=setup(async path=>{if(path.endsWith('/commit')){saves++;return receipt()}return preview()})
  await ui.editor.inspect();assert.ok(ui.editor.preview.value)
  ui.editor.draft.value.name='新名称';assert.equal(ui.editor.preview.value,null)
  await ui.editor.save();assert.equal(saves,0)
  await ui.editor.inspect();await ui.editor.save();assert.equal(saves,1);assert.equal(ui.calls(),1);ui.stop()
})
test('迟到预览不能覆盖已修改文件或切换的账号',async()=>{
  const late=deferred<any>(),ui=setup(async()=>late.promise)
  const task=ui.editor.inspect();ui.editor.draft.value.document='新文件';late.resolve(preview());await task
  assert.equal(ui.editor.preview.value,null);assert.equal(ui.editor.busy.value,false);ui.stop()
})
test('保存超时同键重试，迟到旧结果不能释放待确认请求',async()=>{
  const key=crypto.randomUUID(),saved=rememberSource(key,body()),late=deferred<any>()
  await assert.rejects(sendSource(key,saved,()=>late.promise,5),{code:'REQUEST_TIMEOUT'})
  assert.equal(sourcePending(key),saved);late.resolve(receipt());await Promise.resolve();assert.equal(sourcePending(key),saved)
  await sendSource(key,saved,async()=>receipt());assert.equal(sourcePending(key),null)
})
test('错误成功回执保留请求，旧版本确定拒绝后重预览保留输入',async()=>{
  let mode='invalid';const ui=setup(async path=>{if(path.endsWith('/preview'))return preview();if(mode==='invalid')return {};if(mode==='conflict')throw new ApiError(409,'stale_preview','来源变化');return receipt()})
  await ui.editor.inspect();await ui.editor.save();const old=ui.editor.outstanding.value;assert.ok(old);assert.equal(ui.calls(),0)
  mode='conflict';await ui.editor.save();assert.equal(ui.editor.outstanding.value,null);assert.equal(ui.editor.preview.value,null)
  assert.equal(ui.editor.draft.value.document,document);assert.equal(ui.editor.revisionConflict.value,true);ui.stop()
})
test('卸载恢复原管理员待确认请求；切换账号不展示旧草稿或迟到成功',async()=>{
  const ui=setup(async path=>path.endsWith('/preview')?preview():Promise.reject(new ApiError(503,'database_busy','忙碌')))
  await ui.editor.inspect();await ui.editor.save();const original=ui.editor.outstanding.value,key=ui.identity.value;assert.ok(original);ui.stop()
  const scope=effectScope(),identity=ref(key),late=deferred<any>();let refreshed=0
  const restored=scope.run(()=>useSourceImport(identity,async()=>{refreshed++},async()=>late.promise as any))!
  assert.equal(restored.open.value,true);assert.equal(restored.draft.value.document,document)
  const task=restored.save();identity.value='other';assert.equal(restored.draft.value.document,'');assert.equal(restored.open.value,false)
  late.resolve(receipt());await task;assert.equal(restored.notice.value,'');assert.equal(refreshed,0);restored.dispose();scope.stop()
})
test('UTF8文件上传保留重复JSON键供服务器拒绝，超限及坏编码不替换输入',async()=>{
  const ui=setup(async()=>preview()),text='{"schema_version":1,"schema_version":2,"rules":[]}'
  await ui.editor.readFile(new File([text],'rules.json'));assert.equal(ui.editor.draft.value.document,text)
  await ui.editor.readFile(new File([new Uint8Array([0xff])],'broken.json'));assert.match(ui.editor.error.value,/UTF-8/);assert.equal(ui.editor.draft.value.document,text)
  await ui.editor.readFile(new File([new Uint8Array(262145)],'large.json'));assert.match(ui.editor.error.value,/256/);assert.equal(ui.editor.draft.value.document,text);ui.stop()
})

test('在途保存跨卸载成功后，新组件接收原回执关闭草稿，不能重复新建',async()=>{
  const late=deferred<any>(),ui=setup(async path=>path.endsWith('/preview')?preview():late.promise)
  await ui.editor.inspect();const saving=ui.editor.save(),key=ui.identity.value;ui.stop()
  const scope=effectScope(),identity=ref(key);let refresh=0
  const recovered=scope.run(()=>useSourceImport(identity,async()=>{refresh++}))!
  assert.equal(recovered.open.value,true);assert.ok(recovered.outstanding.value)
  late.resolve(receipt());await saving
  assert.equal(recovered.outstanding.value,null);assert.equal(recovered.open.value,false)
  assert.equal(recovered.notice.value,'已保存候选');assert.equal(recovered.error.value,'');assert.equal(recovered.draft.value.document,'');assert.equal(refresh,1)
  recovered.dispose();scope.stop()
})

test('在途保存跨卸载收到确定拒绝时，不冒充成功，保留草稿要求重预览',async()=>{
  const late=deferred<any>(),ui=setup(async path=>path.endsWith('/preview')?preview():late.promise.then(()=>{throw new ApiError(409,'stale_preview','规则变化')}))
  await ui.editor.inspect();const saving=ui.editor.save(),key=ui.identity.value;ui.stop()
  const scope=effectScope(),identity=ref(key);let refresh=0
  const recovered=scope.run(()=>useSourceImport(identity,async()=>{refresh++}))!
  late.resolve(null);await saving
  assert.equal(recovered.outstanding.value,null);assert.equal(recovered.open.value,true);assert.equal(recovered.notice.value,'')
  assert.equal(recovered.error.value,'规则变化');assert.equal(recovered.draft.value.document,document);assert.equal(refresh,0);assert.equal(recovered.revisionConflict.value,true)
  recovered.dispose();scope.stop()
})

test('迟到文件读取不覆盖手动输入，结束后释放读取状态',async()=>{
  const late=deferred<ArrayBuffer>(),ui=setup(async()=>preview())
  const file={size:50,name:'late.json',arrayBuffer:()=>late.promise} as File
  const reading=ui.editor.readFile(file);assert.equal(ui.editor.fileBusy.value,true)
  ui.editor.draft.value.document='手动保留内容';late.resolve(new TextEncoder().encode('旧文件').buffer);await reading
  assert.equal(ui.editor.draft.value.document,'手动保留内容');assert.match(ui.editor.error.value,/已保留手动输入/)
  assert.equal(ui.editor.fileBusy.value,false);ui.stop()
})
