<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ApiError, request, errorMessage } from '../api'
import { auth, hydrateSession } from '../auth'
import { boundedRequest } from '../billingPending'
import { getRulePending, rememberRulePending, rulePendingKey, submitRulePending } from '../rulePending'
import type { RuleSnapshot as Rule, RuleOperation as Pending } from '../rulePending'
interface Source { id:string; name:string; rule_count:number|null; order_semantics:string }
interface Rules { read_only:boolean; items:Rule[]; sources:Source[]; message:string; candidate_revision:string; limitations?:string[] }
interface Conflict { rule_id:number; message:string }
interface RuleAck { message:string; conflicts:Conflict[]; item:Rule|null; deleted_id?:number; replayed:boolean; candidate_revision:string }
interface Match { domain:string; result:string; final_action:string|null; matched_id:number|null; source_id:string|null; order_semantics:string; message:string; candidate_revision:string; related:{id:number;action:string;kind:string;value:string;scope_domain:string;enabled:boolean;relation:string;matched:boolean;order:number;source_id:string}[]; conflicts:Conflict[]; related_truncated?:boolean }
const data=ref<Rules|null>(null), loading=ref(false), error=ref(''), notice=ref(''), search=ref('')
const editOpen=ref(false), editing=ref<Rule|null>(null), saving=ref(false), editError=ref('')
const sessionVerified=ref(false), conflict=ref(false), latestRule=ref<Rule|null>(null), conflictReady=ref(false)
const form=reactive({action:'proxy',kind:'suffix',value:'',scope_domain:'',enabled:true})
const matchDomain=ref(''), matchBusy=ref(false), matchError=ref(''), match=ref<Match|null>(null)
const deleting=ref<Rule|null>(null), deleteOpen=ref(false)
// 先从服务器核验当前会话，再挂载该管理员的待确认请求。
const identityKey=computed(()=>sessionVerified.value&&auth.ready&&auth.session?.authenticated&&auth.session.user?.is_staff?rulePendingKey(auth.session.user.username):'')
const pending=computed(()=>identityKey.value?getRulePending(identityKey.value):null)
const matchStale=computed(()=>!!match.value&&match.value.candidate_revision!==data.value?.candidate_revision)
const filtered=computed(()=>data.value?.items.filter(r=>`${r.value||''} ${r.scope_domain||''}`.toLowerCase().includes(search.value.trim().toLowerCase()))||[])
const actions:Record<string,string>={client_direct:'本机直连',proxy:'当前代理'}
const kinds:Record<string,string>={exact:'精确域名',suffix:'域名及子域名',regex:'限定域名正则'}
const relations:Record<string,string>={duplicate:'同一规则',parent_covers_child:'父域覆盖',child_covered_by_parent:'子域被覆盖',regex_scope_overlap:'正则范围重叠',exact:'精确匹配',suffix:'后缀匹配',regex:'正则匹配'}
let reads=0, checks=0, generation=0
function clearMatch(){checks++;match.value=null;matchError.value='';matchBusy.value=false}
async function load(){
  if(!identityKey.value)return false
  const token=++reads, current=generation;loading.value=true;error.value='';clearMatch()
  try{const result=await boundedRequest(request<Rules>('/admin/rules'));if(token===reads&&current===generation){data.value=result;return true}}
  catch(e){if(token===reads&&current===generation)error.value=errorMessage(e)}finally{if(token===reads&&current===generation)loading.value=false}
  return false
}
function openEditor(rule?:Rule){
  if(!identityKey.value||data.value?.read_only||saving.value||pending.value)return
  editing.value=rule?{...rule}:null
  Object.assign(form,rule?{action:rule.action,kind:rule.kind,value:rule.value||'',scope_domain:rule.scope_domain||'',enabled:rule.enabled}:{action:'proxy',kind:'suffix',value:'',scope_domain:'',enabled:true})
  editError.value='';conflict.value=false;latestRule.value=null;conflictReady.value=false;editOpen.value=true
}
function closeEditor(){if(!saving.value&&!pending.value)editOpen.value=false}
async function submit(operation:Pending){
  const key=identityKey.value,current=generation
  if(!key||saving.value)return
  const original=rememberRulePending(key,operation)
  saving.value=true;editError.value=''
  try{
    const result=await submitRulePending(key,original,async()=>{
      const value=await request<RuleAck>(original.path,original.method,original.body)
      if(!value||typeof value.message!=='string'||!Array.isArray(value.conflicts)||typeof value.replayed!=='boolean'||typeof value.candidate_revision!=='string'||
        (original.method==='DELETE'?(value.item!==null||value.deleted_id!==original.rule?.id):(!value.item||!Number.isInteger(value.item.id)||!Number.isInteger(value.item.revision))))throw new ApiError(200,'INVALID_RESPONSE','保存回执不完整，结果尚未确认。')
      return value
    })
    if(current!==generation||key!==identityKey.value)return
    editOpen.value=false;deleteOpen.value=false;conflict.value=false
    notice.value=[result.message,...(result.conflicts||[]).map(c=>c.message)].join(' ')
    await load()
  }catch(e){
    if(current!==generation||key!==identityKey.value)return
    editError.value=errorMessage(e)
    if(e instanceof ApiError&&e.status===409&&e.code==='revision_conflict'){
      conflict.value=true;await refreshConflict(editError.value)
    }else if(pending.value)editError.value+=' 请重试原请求确认结果；确认前不要刷新整个页面。'
  }finally{if(current===generation&&key===identityKey.value)saving.value=false}
}
async function refreshConflict(message='规则版本已变化。'){
  const current=generation,id=deleteOpen.value?deleting.value?.id:editing.value?.id
  conflictReady.value=false;latestRule.value=null
  const refreshed=await load()
  if(current!==generation)return
  if(refreshed){latestRule.value=data.value?.items.find(row=>row.id===id)||null;conflictReady.value=true}
  editError.value=message+(refreshed?(latestRule.value?' 当前规则已读取，输入已保留；请核对最新内容后继续。':' 当前规则已不存在，输入已保留。'):' 输入已保留，刷新失败；请重新读取当前规则。')
}
function adoptLatest(){
  if(!conflictReady.value||!latestRule.value||saving.value||loading.value||data.value?.read_only)return
  if(deleteOpen.value)deleting.value={...latestRule.value}
  else editing.value={...latestRule.value}
  conflict.value=false;editError.value='已采用核对后的最新版本；输入仍保留，请再次确认操作。'
}
async function save(){
  if(saving.value)return
  if(pending.value)return submit(pending.value)
  if(!identityKey.value||!data.value||data.value.read_only)return
  if(conflict.value){editError.value='请先核对最新规则并采用当前版本，输入已保留。';return}
  const row=editing.value
  return submit({path:'/admin/rules'+(row?'/'+row.id:''),method:row?'PATCH':'POST',body:{...form,scope_domain:form.kind==='regex'?form.scope_domain:'',...(row?{revision:row.revision}:{}),idempotency_key:crypto.randomUUID()},rule:row?{...row}:null,draft:{...form}})
}
function askDelete(rule:Rule){if(!identityKey.value||data.value?.read_only||saving.value||pending.value)return;deleting.value={...rule};editError.value='';conflict.value=false;latestRule.value=null;conflictReady.value=false;deleteOpen.value=true}
async function remove(){
  if(saving.value)return
  if(pending.value)return submit(pending.value)
  if(!identityKey.value||!data.value||data.value.read_only)return
  if(conflict.value){editError.value='请先核对最新规则并采用当前版本，再确认删除。';return}
  if(deleting.value)return submit({path:'/admin/rules/'+deleting.value.id,method:'DELETE',body:{revision:deleting.value.revision,confirm:true,idempotency_key:crypto.randomUUID()},rule:{...deleting.value},draft:{...form}})
}
async function toggle(rule:Rule){if(!identityKey.value||data.value?.read_only||saving.value||pending.value)return;openEditor(rule);form.enabled=!rule.enabled;await save()}
async function inspect(){
  if(!identityKey.value)return
  const token=++checks,current=generation;matchBusy.value=true;matchError.value='';match.value=null
  try{const result=await boundedRequest(request<Match>('/admin/rules/preview','POST',{domain:matchDomain.value}));if(token===checks&&current===generation)match.value=result}
  catch(e){if(token===checks&&current===generation)matchError.value=errorMessage(e)}finally{if(token===checks&&current===generation)matchBusy.value=false}
}
watch(matchDomain,clearMatch)
watch(identityKey,key=>{
  generation++;reads++;clearMatch();data.value=null;error.value='';notice.value='';saving.value=false;loading.value=false
  matchDomain.value='';search.value=''
  editOpen.value=false;deleteOpen.value=false;editing.value=null;deleting.value=null;conflict.value=false;latestRule.value=null;conflictReady.value=false;editError.value=''
  Object.assign(form,{action:'proxy',kind:'suffix',value:'',scope_domain:'',enabled:true})
  if(!key)return
  const original=getRulePending(key)
  if(original){Object.assign(form,original.draft);if(original.method==='DELETE'){deleting.value=original.rule;deleteOpen.value=true}else{editing.value=original.rule;editOpen.value=true}editError.value='上次请求结果尚未确认，请重试原请求；不要刷新整个页面。'}
  void load()
},{immediate:true,flush:'sync'})
watch(()=>({key:identityKey.value,operation:pending.value}),(value,previous)=>{
  if(value.key===previous.key&&!value.operation&&previous.operation&&!saving.value&&value.key){editOpen.value=false;deleteOpen.value=false;notice.value='此前请求已返回，候选列表正在重新核对。';void load()}
})
onMounted(async()=>{try{await hydrateSession(true);sessionVerified.value=true;if(!identityKey.value)error.value='请使用有效管理员会话。'}catch(e){error.value=errorMessage(e)}})
onUnmounted(()=>{generation++;reads++;checks++})
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员 · 规则管理</p><h1>代理规则</h1><p class="muted">编辑本地候选，检查域名覆盖和冲突。</p></div><div><el-button :loading="loading" @click="load">刷新列表</el-button><el-button type="primary" :disabled="!data||data.read_only||saving||!!pending" @click="openEditor()">添加规则</el-button></div></div>
  <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="spaced" />
  <el-alert v-if="pending" title="上次规则操作仍待确认，重试会使用原请求和幂等键。可离开后返回本页；整页刷新或应用重启会丢失内存记录，请先确认结果。" type="warning" show-icon :closable="false" class="spaced" />
  <el-alert v-if="notice" :title="notice" type="success" show-icon class="spaced" @close="notice=''" />
  <el-skeleton v-if="loading&&!data" :rows="6" animated />
  <template v-if="data">
    <el-alert :title="data.message" type="info" :closable="false" show-icon class="spaced" />
    <section class="surface admin-panel"><h2>域名命中检查</h2><p class="muted small">查看当前候选的命中项和相关父子域。未命中自建规则时，最终结果还取决于尚未接入的基础规则。</p>
      <form class="rule-inspect" @submit.prevent="inspect"><el-input v-model="matchDomain" aria-label="检查域名" placeholder="例如 api.example.com" clearable /><el-button native-type="submit" :loading="matchBusy" :disabled="!matchDomain.trim()">检查匹配</el-button></form>
      <el-alert v-if="matchError" :title="matchError" type="error" :closable="false" class="spaced" />
      <div v-if="match" class="inline-note"><strong>{{match.domain}}：{{match.result==='invalid_candidate'?'候选含无效规则，无法确定':match.final_action?actions[match.final_action]:'未命中自建规则，最终动作待确认'}}</strong><p>{{match.matched_id?'命中规则 #'+match.matched_id:match.source_id==='protected'?'命中内置保护校验':''}}</p><p class="small">{{match.order_semantics}}</p><p v-for="conflict in match.conflicts" :key="conflict.rule_id+conflict.message">{{conflict.message}}</p>
        <p class="small">候选版本：{{match.candidate_revision.slice(0,12)}} · 来源：{{match.source_id==='protected'?'内置保护校验':match.source_id==='custom'?'数据库自建候选':'未命中已知候选来源'}}</p><p v-if="matchStale" class="small">匹配快照与当前列表版本不同，请刷新列表后重新检查。</p>
        <el-table v-if="match.related.length" :data="match.related"><el-table-column label="相关规则" prop="value" min-width="220" /><el-table-column label="动作"><template #default="{row}">{{actions[row.action]}}</template></el-table-column><el-table-column label="关系"><template #default="{row}">{{relations[row.relation]||row.relation}}</template></el-table-column><el-table-column label="域名匹配"><template #default="{row}">{{row.enabled?(row.matched?'匹配':'不匹配'):'已禁用，不参与命中'}}</template></el-table-column></el-table>
        <p v-if="match.related_truncated" class="small">相关条目较多，仅展示部分。</p><p class="small muted">{{match.message}}</p>
      </div>
    </section>
    <section class="surface admin-panel"><div class="section-title"><h2>自建规则</h2><el-input v-model="search" aria-label="搜索规则" placeholder="搜索域名或正则范围" clearable class="rule-search" /></div>
      <p v-if="!data.items.length" class="inline-note">当前本地库没有自建规则；这不代表生产规则被删除。可添加候选进行验证。</p>
      <div class="table-scroll"><el-table :data="filtered" row-key="id" empty-text="没有匹配规则"><el-table-column label="规则" min-width="230"><template #default="{row}"><strong>{{row.value||'无效条目，需修正'}}</strong><div v-if="row.scope_domain" class="small muted">范围：{{row.scope_domain}}</div></template></el-table-column><el-table-column label="类型" min-width="140"><template #default="{row}">{{kinds[row.kind]}}</template></el-table-column><el-table-column label="动作" min-width="120"><template #default="{row}">{{actions[row.action]}}</template></el-table-column><el-table-column label="启用" min-width="100"><template #default="{row}"><el-switch :model-value="row.enabled" :disabled="data.read_only||saving||!!pending||row.validation!=='valid'" :aria-label="'启停规则 '+row.value" @change="toggle(row)" /></template></el-table-column><el-table-column label="操作" min-width="160"><template #default="{row}"><el-button text :disabled="data.read_only||saving||!!pending" @click="openEditor(row)">编辑</el-button><el-button text type="danger" :disabled="data.read_only||saving||!!pending" @click="askDelete(row)">删除</el-button></template></el-table-column></el-table></div>
    </section>
    <el-collapse class="spaced"><el-collapse-item title="规则来源与当前支持范围" name="sources"><div v-for="source in data.sources" :key="source.id" class="inline-note"><strong>{{source.name}}</strong> · {{source.rule_count===null?'未接入':source.rule_count+' 条'}}<p>{{source.order_semantics}}</p></div><p v-for="item in data.limitations" :key="item" class="small muted">{{item}}</p></el-collapse-item></el-collapse>
  </template>
  <el-drawer :model-value="editOpen" :title="editing?'编辑规则':'添加规则'" size="min(520px, 100vw)" :close-on-click-modal="false" :close-on-press-escape="!saving&&!pending" :show-close="!saving&&!pending" @close="closeEditor">
    <el-alert v-if="editError" :title="editError" type="error" :closable="false" show-icon class="spaced" />
    <div v-if="conflict" class="inline-note"><p v-if="latestRule">最新规则 #{{latestRule.id}} · 版本 {{latestRule.revision}}：{{latestRule.value||'无效条目'}} · {{actions[latestRule.action]}} · {{latestRule.enabled?'启用':'禁用'}}</p><el-button :disabled="saving||loading" @click="refreshConflict()">重新读取当前规则</el-button><el-button :disabled="saving||loading||!conflictReady||!latestRule||!!data?.read_only" @click="adoptLatest">核对后采用最新版本</el-button></div>
    <el-form label-position="top" :disabled="saving||!!pending" @submit.prevent="save"><el-form-item label="动作"><el-radio-group v-model="form.action"><el-radio-button value="proxy">当前代理</el-radio-button><el-radio-button value="client_direct">本机直连</el-radio-button></el-radio-group></el-form-item><el-form-item label="匹配类型"><el-select v-model="form.kind" aria-label="匹配类型"><el-option v-for="(label,key) in kinds" :key="key" :value="key" :label="label" /></el-select></el-form-item><el-form-item label="域名或限定正则"><el-input v-model="form.value" aria-label="规则内容" placeholder="example.com" maxlength="253" /></el-form-item><el-form-item v-if="form.kind==='regex'" label="正则限定域名范围"><el-input v-model="form.scope_domain" aria-label="正则范围" placeholder="example.com" maxlength="253" /></el-form-item><el-form-item label="启用规则"><el-switch v-model="form.enabled" aria-label="启用规则" /></el-form-item></el-form>
    <p class="small muted">只保存候选。不会自动发布到服务器或改变设备配置。</p><template #footer><el-button :disabled="saving||!!pending" @click="closeEditor">取消</el-button><el-button type="primary" :loading="saving" :disabled="conflict&&!pending" @click="save">{{pending?'重试原请求':'保存候选'}}</el-button></template>
  </el-drawer>
  <el-dialog v-model="deleteOpen" title="删除候选规则" width="min(480px, 94vw)" :close-on-click-modal="false" :close-on-press-escape="!saving&&!pending" :show-close="!saving&&!pending"><p>删除 {{deleting?.value}}？已发布的配置不会随此操作改变。</p><el-alert v-if="editError" :title="editError" type="error" :closable="false" /><div v-if="conflict" class="inline-note"><p v-if="latestRule">最新规则 #{{latestRule.id}} · 版本 {{latestRule.revision}}：{{latestRule.value||'无效条目'}} · {{actions[latestRule.action]}}</p><el-button :disabled="saving||loading" @click="refreshConflict()">重新读取当前规则</el-button><el-button :disabled="saving||loading||!conflictReady||!latestRule||!!data?.read_only" @click="adoptLatest">核对后采用最新版本</el-button></div><template #footer><el-button :disabled="saving||!!pending" @click="deleteOpen=false">取消</el-button><el-button type="danger" :loading="saving" :disabled="conflict&&!pending" @click="remove">{{pending?'重试原请求':'确认删除'}}</el-button></template></el-dialog>
</template>
<style scoped>
.rule-inspect { display:flex; gap:12px; max-width:640px; }
.admin-panel { margin-bottom:20px; }
</style>
