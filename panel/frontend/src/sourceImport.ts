import { computed, ref, shallowReactive, watch, type Ref } from 'vue'
import { ApiError, errorMessage, request } from './api.ts'
import { boundedRequest } from './billingPending.ts'

export interface SourceRule { action:string; kind:string; value:string; scope_domain:string; enabled:boolean }
export interface RuleSource { source_id:string; name:string; revision:number; state:string; sha256:string; count:number; updated_at:string }
export interface SourcePreview {
  preview_token:string|null; can_commit:boolean; sha256:string; count:number; message:string
  items:SourceRule[]; diff:{added:SourceRule[];removed:SourceRule[];modified:{before:SourceRule;after:SourceRule}[];counts:Record<string,number>;order_changed?:boolean}
  conflicts:{message:string;relation:string;severity:string;source_name?:string;candidate_value?:string;other_value?:string;candidate_action?:string;other_action?:string}[]; conflicts_truncated?:boolean
}
export interface SourceDraft { name:string; source_id:string|null; expected_revision:number; document:string }
export interface SourceSave extends SourceDraft { preview_token:string; idempotency_key:string }
interface SourceReceipt { item:RuleSource; version:{revision:number;sha256:string}; replayed:boolean; message:string }
// 仅在本应用内存保留未确认请求；按核验后的管理员隔离，不写浏览器存储。
const pending=shallowReactive(new Map<string,Readonly<SourceSave>>())
const running=new Map<string,Promise<SourceReceipt>>()
const settled=new Map<string,{id:string;receipt?:SourceReceipt;error?:unknown}>()
export function sourcePending(key:string){return pending.get(key)||null}
export function sourceRejected(error:unknown){
  return error instanceof ApiError && [404,409,413,415,422].includes(error.status) && [
    'invalid_json','invalid_fields','invalid_document','document_too_large','rule_limit','invalid_rule',
    'duplicate_rule','not_found','revision_conflict','stale_preview','invalid_preview','preview_expired',
    'idempotency_conflict','unsupported_media_type','invalid_existing_source',
  ].includes(error.code)
}
export function rememberSource(key:string,body:SourceSave){
  if(!pending.has(key)){settled.delete(key);pending.set(key,Object.freeze({...body}))}
  return pending.get(key)!
}
export function sendSource(key:string,body:Readonly<SourceSave>,send:()=>Promise<SourceReceipt>,timeout=15000){
  const operation=JSON.stringify([key,body.idempotency_key]),existing=running.get(operation)
  if(existing)return existing
  const clear=()=>{if(pending.get(key)?.idempotency_key===body.idempotency_key)pending.delete(key)}
  const attempt=Promise.resolve().then(()=>boundedRequest(send(),timeout)).then(result=>{
    if(!result?.item?.source_id||!Number.isSafeInteger(result.item.revision)||result.item.revision<1||
      result.version?.revision!==result.item.revision||typeof result.version.sha256!=='string'||
      typeof result.replayed!=='boolean'||typeof result.message!=='string')
      throw new ApiError(200,'INVALID_RESPONSE','保存回执不完整，结果尚未确认。')
    settled.set(key,{id:body.idempotency_key,receipt:result});clear();return result
  }).catch(error=>{if(sourceRejected(error)){settled.set(key,{id:body.idempotency_key,error});clear()}throw error}).finally(()=>{if(running.get(operation)===attempt)running.delete(operation)})
  running.set(operation,attempt);return attempt
}
const empty=():SourceDraft=>({name:'',source_id:null,expected_revision:0,document:''})
export function useSourceImport(identity:Ref<string>,onSaved:()=>Promise<unknown>,send:typeof request=request){
  const draft=ref<SourceDraft>(empty()),open=ref(false),busy=ref(false),fileBusy=ref(false),error=ref(''),notice=ref('')
  const preview=ref<SourcePreview|null>(null),fileName=ref(''),revisionConflict=ref(false)
  const outstanding=computed(()=>identity.value?sourcePending(identity.value):null)
  let generation=0,previewRun=0,fileRun=0
  watch(identity,()=>{
    generation++;previewRun++;fileRun++;busy.value=false;fileBusy.value=false;preview.value=null;error.value='';notice.value='';fileName.value='';revisionConflict.value=false
    const previous=outstanding.value
    draft.value=previous?{name:previous.name,source_id:previous.source_id,expected_revision:previous.expected_revision,document:previous.document}:empty()
    open.value=!!previous
    if(previous)error.value='上次保存结果尚未确认，请重试原请求；确认前不要刷新整个页面。'
  },{immediate:true,flush:'sync'})
  watch(draft,()=>{previewRun++;preview.value=null},{deep:true,flush:'sync'})
  // 导航返回的新组件也接收旧组件发起的在途回执，防止已成功请求变成可重复新建草稿。
  watch(outstanding,(value,previous)=>{
    if(value||!previous||busy.value)return
    const result=settled.get(identity.value)
    if(!result||result.id!==previous.idempotency_key)return
    preview.value=null
    if(result.receipt){open.value=false;draft.value=empty();error.value='';notice.value=result.receipt.message;void onSaved()}
    else {error.value=errorMessage(result.error);revisionConflict.value=result.error instanceof ApiError&&['revision_conflict','stale_preview'].includes(result.error.code)}
  },{flush:'sync'})
  function begin(source?:RuleSource){
    if(!identity.value||busy.value||outstanding.value)return
    fileRun++;fileBusy.value=false;draft.value={...empty(),name:source?.name||'',source_id:source?.source_id||null,expected_revision:source?.revision||0}
    fileName.value='';error.value='';revisionConflict.value=false;open.value=true
  }
  function close(){if(!busy.value&&!outstanding.value){open.value=false;previewRun++;fileRun++;fileBusy.value=false}}
  async function readFile(file:File){
    if(busy.value||outstanding.value)return
    const token=++fileRun,current=generation,documentBefore=draft.value.document;fileBusy.value=true;error.value='';preview.value=null;previewRun++
    try{
      if(file.size>262144)throw new ApiError(413,'document_too_large','文件不能超过256 KiB。')
      const bytes=await file.arrayBuffer()
      if(token!==fileRun||current!==generation)return
      if(draft.value.document!==documentBefore){error.value='读取文件期间内容已修改，已保留手动输入；如需导入请重新选择文件。';return}
      try{draft.value.document=new TextDecoder('utf-8',{fatal:true}).decode(bytes)}
      catch{throw new ApiError(422,'invalid_document','请上传UTF-8编码的JSON文件。')}
      fileName.value=file.name
    }catch(e){if(token===fileRun&&current===generation)error.value=errorMessage(e)}
    finally{if(token===fileRun&&current===generation)fileBusy.value=false}
  }
  async function inspect(){
    if(!identity.value||busy.value||fileBusy.value||outstanding.value)return
    const token=++previewRun,current=generation;busy.value=true;error.value='';preview.value=null
    try{
      const result=await boundedRequest(send<SourcePreview>('/admin/rule-sources/preview','POST',{...draft.value}))
      if(token===previewRun&&current===generation){
        if(!result?.diff||!Array.isArray(result.conflicts)||typeof result.can_commit!=='boolean')throw new ApiError(200,'INVALID_RESPONSE','预览内容不完整。')
        preview.value=result;revisionConflict.value=false
      }
    }catch(e){if(token===previewRun&&current===generation){error.value=errorMessage(e);revisionConflict.value=e instanceof ApiError&&e.code==='revision_conflict'}}
    finally{if(current===generation)busy.value=false}
  }
  async function refreshRevision(){
    if(!identity.value||!draft.value.source_id||busy.value||outstanding.value)return
    const current=generation;busy.value=true;error.value=''
    try{
      const result=await boundedRequest(send<{item:RuleSource}>('/admin/rule-sources/'+encodeURIComponent(draft.value.source_id)))
      if(current===generation){draft.value.expected_revision=result.item.revision;revisionConflict.value=false;error.value='当前版本已读取，文件内容保留；请重新预览差异。'}
    }catch(e){if(current===generation)error.value=errorMessage(e)}finally{if(current===generation)busy.value=false}
  }
  async function save(){
    const key=identity.value,current=generation
    if(!key||busy.value||fileBusy.value)return
    const previous=outstanding.value
    if(!previous&&(!preview.value?.can_commit||!preview.value.preview_token))return
    const body=previous||rememberSource(key,{...draft.value,preview_token:preview.value!.preview_token!,idempotency_key:crypto.randomUUID()})
    busy.value=true;error.value=''
    try{
      const result=await sendSource(key,body,()=>send<SourceReceipt>('/admin/rule-sources/commit','POST',body))
      if(current!==generation||key!==identity.value)return
      notice.value=result.message;open.value=false;preview.value=null;await onSaved()
    }catch(e){
      if(current!==generation||key!==identity.value)return
      error.value=errorMessage(e)
      if(outstanding.value)error.value+=' 请重试原请求确认结果；不要刷新整个页面。'
      else {preview.value=null;revisionConflict.value=e instanceof ApiError&&['revision_conflict','stale_preview'].includes(e.code)}
    }finally{if(current===generation)busy.value=false}
  }
  function dispose(){generation++;previewRun++;fileRun++}
  return {draft,open,busy,fileBusy,error,notice,preview,fileName,revisionConflict,outstanding,begin,close,readFile,inspect,refreshRevision,save,dispose}
}
