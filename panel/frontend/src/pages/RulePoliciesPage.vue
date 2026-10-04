<script setup lang="ts">
import RuleSectionNav from '../components/RuleSectionNav.vue'
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { auth, hydrateSession } from '../auth'
import { request, errorMessage } from '../api'
import { boundedRequest } from '../billingPending'
import { capturePolicySave, moveComponent, submitPolicyPending, uncertainFailure, getPolicyPending, rememberPolicyPending, clearPolicyPending } from '../rulePolicies'
import type { Binding, Candidate, Component, MatchExplanation, PolicyDetail, PolicyDraft, PolicyItem, PolicyOptions, PolicyPending } from '../rulePolicies'

const isProduction=computed(()=>auth.session?.environment?.kind==='production')
const verified=ref(false),identity=computed(()=>verified.value&&auth.session?.authenticated&&auth.session.user?.is_staff?auth.session.user.username:'')
const listing=ref<PolicyItem[]>([]),options=ref<(PolicyOptions & {production_publish_available?:boolean;https_import_available?:boolean})|null>(null),detail=ref<PolicyDetail|null>(null)
const loading=ref(false),busy=ref(false),error=ref(''),notice=ref(''),open=ref(false),editing=ref<PolicyItem|null>(null)
const draft=reactive<PolicyDraft>({name:'',components:[],overrides:{}}),selectedComponent=ref('')
const pending=computed(()=>identity.value?getPolicyPending(identity.value):null),domain=ref(''),match=ref<MatchExplanation|null>(null),candidate=ref<Candidate|null>(null)
const serviceQuery=ref(''),services=ref<{id:string;source_type:string;name:string;user?:{username:string};state:string}[]>([]),selectedService=ref(''),selectedBinding=ref('')
const overrideTarget=ref(''),overrideReplacement=ref(''),readOnly=computed(()=>options.value?.read_only!==false),locked=computed(()=>busy.value||!!pending.value)
let generation=0,readGeneration=0,detailGeneration=0
const actions:Record<string,string>={proxy:'当前代理',client_direct:'本机直连'}
const choices=computed(()=>[
  ...(options.value?.sources||[]).map(row=>({key:`source/${row.source_id}/${row.revision}`,label:`${row.name} · v${row.revision} · ${row.count}条`,component:{type:'source',source_id:row.source_id,revision:row.revision,enabled:true} as Component})),
  ...(options.value?.custom||[]).map(row=>({key:row.key,label:`自建 · ${actions[row.rule.action]} · ${row.rule.value}`,component:{type:'custom',rule_id:row.rule_id,revision:row.revision,enabled:true} as Component})),
])
function componentKey(row:Component){return row.type==='source'?`source/${row.source_id}/${row.revision}`:`custom/${row.rule_id}/${row.revision}`}
function componentLabel(row:Component){return choices.value.find(x=>x.key===componentKey(row))?.label||componentKey(row)}
const sourceKeys=computed(()=>draft.components.flatMap(row=>row.type==='source'?(options.value?.sources.find(source=>source.source_id===row.source_id&&source.revision===row.revision)?.keys||[]):[]))
const customKeys=computed(()=>draft.components.flatMap(row=>row.type==='custom'&&row.enabled?(options.value?.custom.filter(custom=>custom.rule_id===row.rule_id&&custom.revision===row.revision)||[]):[]))
const chosenBinding=computed(()=>detail.value?.bindings.find(row=>row.binding_id===selectedBinding.value)||null)

async function load(){
  if(!identity.value)return
  const own=identity.value,token=++readGeneration;loading.value=true;error.value=''
  try{const [rows,choices]=await Promise.all([boundedRequest(request<{items:PolicyItem[]}>('/admin/rule-policies')),boundedRequest(request<PolicyOptions>('/admin/rule-policies/options'))]);if(token===readGeneration&&own===identity.value){listing.value=rows.items;options.value=choices}}
  catch(e){if(token===readGeneration&&own===identity.value)error.value=errorMessage(e)}finally{if(token===readGeneration&&own===identity.value)loading.value=false}
}
async function show(item:PolicyItem,revision?:number){
  const own=identity.value,token=++detailGeneration;error.value='';match.value=null;candidate.value=null
  try{const data=await boundedRequest(request<PolicyDetail>(`/admin/rule-policies/${item.policy_id}`+(revision?`?revision=${revision}`:'')));if(token===detailGeneration&&own===identity.value){detail.value=data;selectedBinding.value=data.bindings[0]?.binding_id||''}}
  catch(e){if(token===detailGeneration&&own===identity.value)error.value=errorMessage(e)}
}
function begin(item:PolicyDetail|null=null){
  if(locked.value||readOnly.value)return
  editing.value=item?.item||null;draft.name=item?.item.name||''
  draft.components=JSON.parse(JSON.stringify(item?.version.document.components||[]));draft.overrides=JSON.parse(JSON.stringify(item?.version.document.overrides||{}))
  selectedComponent.value='';overrideTarget.value='';overrideReplacement.value='';open.value=true;error.value=''
}
function add(){const row=choices.value.find(x=>x.key===selectedComponent.value);if(row&&!draft.components.some(x=>componentKey(x)===row.key))draft.components.push(structuredClone(row.component));selectedComponent.value=''}
function remove(index:number){draft.components.splice(index,1);const allowed=new Set([...sourceKeys.value,...customKeys.value].map(x=>x.key));draft.overrides=Object.fromEntries(Object.entries(draft.overrides).filter(([key,value])=>allowed.has(key)&&(value===null||allowed.has(value))))}
function setOverride(){if(overrideTarget.value)draft.overrides[overrideTarget.value]=overrideReplacement.value||null;overrideTarget.value='';overrideReplacement.value=''}
async function mutate(operation:PolicyPending){
  if(!identity.value||busy.value)return
  const own=identity.value,token=generation;operation=rememberPolicyPending(own,operation);busy.value=true;error.value='';notice.value=''
  try{
    const result=await boundedRequest(submitPolicyPending<{item?:PolicyItem;binding?:Binding;candidate?:Candidate;message:string}>(operation))
    if(own!==identity.value||token!==generation)return
    clearPolicyPending(own);notice.value=result.message
    if(result.item){open.value=false;await load();await show(result.item)}
    else if(detail.value){const item=detail.value.item;await show(item);if(result.candidate)candidate.value=result.candidate;if(result.binding)selectedBinding.value=result.binding.binding_id}
  }catch(e){if(own===identity.value&&token===generation){error.value=errorMessage(e);if(!uncertainFailure(e))clearPolicyPending(own)}}
  finally{if(own===identity.value&&token===generation)busy.value=false}
}
function save(){void mutate(pending.value||capturePolicySave(draft,editing.value,crypto.randomUUID()))}
async function refreshEditRevision(){
  if(!editing.value||locked.value)return
  const own=identity.value,token=generation
  try{const value=await boundedRequest(request<PolicyDetail>(`/admin/rule-policies/${editing.value.policy_id}`));if(own===identity.value&&token===generation){editing.value=value.item;notice.value='已读取当前修订号；输入保留，请核对是否仍适合保存。';await load()}}catch(e){if(own===identity.value)error.value=errorMessage(e)}
}
async function check(){
  if(!detail.value||locked.value)return
  const own=identity.value,token=detailGeneration,item=detail.value,input=domain.value;busy.value=true;error.value=''
  try{const value=await boundedRequest(request<MatchExplanation>(`/admin/rule-policies/${item.item.policy_id}/preview`,'POST',{domain:input,revision:item.version.revision}));if(own===identity.value&&token===detailGeneration&&domain.value===input)match.value=value}
  catch(e){if(own===identity.value&&token===detailGeneration)error.value=errorMessage(e)}finally{if(own===identity.value)busy.value=false}
}
async function searchServices(){
  if(!identity.value)return
  const own=identity.value,token=generation;error.value=''
  try{const value=await boundedRequest(request<{items:typeof services.value}>('/admin/services?'+new URLSearchParams({q:serviceQuery.value,page_size:'25'})));if(own===identity.value&&token===generation){services.value=value.items;selectedService.value=''}}catch(e){if(own===identity.value)error.value=errorMessage(e)}
}
function bind(){
  const service=services.value.find(row=>row.source_type+'/'+row.id===selectedService.value);if(!detail.value||!service)return
  const existing=detail.value.bindings.find(row=>row.service_id===service.id&&row.service_source===service.source_type)
  void mutate({path:`/admin/rule-policies/${detail.value.item.policy_id}/bind`,method:'POST',body:{service_id:service.id,service_source:service.source_type,expected_binding_revision:existing?.revision||0,idempotency_key:crypto.randomUUID()}})
}
function compile(){
  const binding=chosenBinding.value;if(!detail.value||!binding)return
  void mutate({path:`/admin/rule-policies/${detail.value.item.policy_id}/compile`,method:'POST',body:{revision:detail.value.version.revision,binding_id:binding.binding_id,expected_candidate_id:binding.current_candidate_id,expected_fence:binding.fence,idempotency_key:crypto.randomUUID()}})
}
async function showCandidate(){
  const binding=chosenBinding.value;if(!detail.value||!binding?.current_candidate_id)return
  const own=identity.value,token=detailGeneration
  try{const value=await boundedRequest(request<{candidate:Candidate}>(`/admin/rule-policies/${detail.value.item.policy_id}/candidates/${binding.current_candidate_id}`));if(own===identity.value&&token===detailGeneration)candidate.value=value.candidate}catch(e){if(own===identity.value)error.value=errorMessage(e)}
}
watch(domain,()=>{match.value=null})
watch(selectedBinding,()=>{candidate.value=null})
watch(identity,()=>{generation++;readGeneration++;detailGeneration++;listing.value=[];options.value=null;detail.value=null;candidate.value=null;services.value=[];open.value=false;busy.value=false;loading.value=false;error.value='';notice.value='';if(identity.value)void load()})
onMounted(async()=>{try{await hydrateSession(true);verified.value=true}catch(e){error.value=errorMessage(e)}})
onUnmounted(()=>{generation++;readGeneration++;detailGeneration++})
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员工作区 · 代理规则</p><h1>规则方案</h1><p class="muted">将来源与自建规则组成固定版本，查看顺序、覆盖和命中。</p></div><el-button v-if="options?.read_only===false" type="primary" :disabled="locked" @click="begin()">新建方案</el-button></div>
  <RuleSectionNav active="policies" :read-only="options?.read_only" :production="isProduction" :publish-available="options?.production_publish_available" :https-available="options?.https_import_available" />
  <el-alert v-if="error" :title="error" type="error" :closable="false" class="spaced" />
  <el-alert v-if="notice" :title="notice" type="success" :closable="false" class="spaced" />
  <el-alert v-if="pending" title="上次保存结果尚未确认，请重试同一请求；当前输入已锁定。" type="warning" :closable="false"><el-button :loading="busy" @click="mutate(pending!)">重试原请求</el-button></el-alert>
  <section class="surface admin-panel">
    <p class="inline-note">方案版本会固定来源与自建条目；来源更新不会覆盖此版本。编译材料尚不代表发布成功，原链接和客户端保持原状态。</p>
    <el-button :loading="loading" :disabled="locked" @click="load">刷新方案和来源</el-button>
    <el-table v-loading="loading" :data="listing" row-key="policy_id" :empty-text="readOnly?'当前没有可查看的规则方案。':'尚无规则方案，可选择已有来源或自建规则创建。'"><el-table-column label="方案" prop="name" /><el-table-column label="版本" prop="revision" width="100" /><el-table-column label="操作"><template #default="{row}"><el-button text :disabled="locked" @click="show(row)">查看与检查</el-button></template></el-table-column></el-table>
  </section>
  <section v-if="detail" class="surface admin-panel spaced">
    <div class="policy-toolbar"><h2>{{detail.item.name}} · v{{detail.version.revision}}</h2><el-select :model-value="detail.version.revision" aria-label="方案历史版本" :disabled="locked" @change="(revision:number)=>show(detail!.item,revision)"><el-option v-for="version in detail.history" :key="version.revision" :value="version.revision" :label="'v'+version.revision" /></el-select><el-button v-if="!readOnly" :disabled="locked||detail.version.revision!==detail.item.revision" @click="begin(detail)">编辑为下一版本</el-button></div>
    <el-table :data="detail.version.document.entries" max-height="400" empty-text="此方案无启用附加规则，默认仍走代理。"><el-table-column type="index" label="顺序" width="70" /><el-table-column prop="source_name" label="来源" min-width="120" /><el-table-column prop="rule.value" label="匹配内容" min-width="220" /><el-table-column label="动作"><template #default="{row}">{{actions[row.rule.action]}}</template></el-table-column><el-table-column label="状态"><template #default="{row}">{{row.enabled?'启用':row.overridden_by!==undefined?'显式覆盖/禁用':'停用'}}</template></el-table-column></el-table>
    <form class="policy-toolbar spaced" @submit.prevent="check"><el-input v-model="domain" placeholder="输入域名查看组合命中" aria-label="组合命中域名" maxlength="253" /><el-button native-type="submit" :loading="busy" :disabled="locked||!domain.trim()">检查当前版本</el-button></form>
    <div v-if="match" class="inline-note"><strong>{{actions[match.final_action]}} · {{match.reason}}</strong><p>这是方案v{{match.revision}}的解释，尚不代表客户端应用。</p><ol><li v-for="row in match.matches" :key="row.key">第{{row.order}}项 · {{row.source_name}} · {{actions[row.action]}} · {{row.enabled?'启用':row.overridden?'被覆盖':'停用'}}</li></ol></div>
    <template v-if="!readOnly"><h3>绑定服务</h3><p class="small muted">使用同一份服务编号与来源，只绑定规则选择，不创建套餐或改变额度。</p>
    <form class="policy-toolbar" @submit.prevent="searchServices"><el-input v-model="serviceQuery" placeholder="搜索服务用户或编号" aria-label="搜索绑定服务" /><el-button native-type="submit" :disabled="locked||readOnly">查找服务</el-button></form>
    <div class="policy-toolbar spaced"><el-select v-model="selectedService" aria-label="选择绑定服务" :disabled="locked||readOnly" placeholder="选择已核对的服务"><el-option v-for="service in services" :key="service.source_type+'/'+service.id" :value="service.source_type+'/'+service.id" :label="(service.user?.username||'')+' · '+service.id+' · '+service.source_type" :disabled="service.state==='mapping_required'" /></el-select><el-button :disabled="locked||readOnly||!selectedService" @click="bind">绑定当前方案</el-button></div>
    </template>
    <h3>发布检查材料</h3><p class="small muted">现有P8生成器最多接受200条启用规则，并要求代理项在直连项之前。后台会拒绝不兼容顺序；生产发布需核对真实基础来源和唯一发布者。</p>
    <div class="policy-toolbar"><el-select v-model="selectedBinding" aria-label="选择已绑定服务" :disabled="locked"><el-option v-for="binding in detail.bindings" :key="binding.binding_id" :value="binding.binding_id" :label="binding.service_source+' · '+binding.service_id" /></el-select><el-button v-if="!readOnly" :disabled="locked||!chosenBinding||detail.version.revision!==detail.item.revision" :loading="busy" @click="compile">生成P8输入</el-button><el-button :disabled="locked||!chosenBinding?.current_candidate_id" @click="showCandidate">查看上次材料</el-button></div>
    <div v-if="candidate" class="inline-note"><p>编译候选 #{{candidate.fence}} · 未生产发布</p><p class="small">材料摘要：{{candidate.sha256}}</p><el-collapse><el-collapse-item title="查看生成器输入与版本清单" name="artifact"><pre class="policy-json">{{JSON.stringify(candidate.artifact,null,2)}}</pre><pre class="policy-json">{{JSON.stringify(candidate.manifest,null,2)}}</pre></el-collapse-item></el-collapse></div>
  </section>
  <el-drawer :model-value="open" :title="editing?'编辑下一方案版本':'新建规则方案'" size="min(920px,100vw)" :close-on-click-modal="false" :show-close="!locked" :close-on-press-escape="!locked" @close="open=false">
    <el-form label-position="top" :disabled="locked"><el-form-item label="方案名称（可留空）"><el-input v-model="draft.name" maxlength="100" placeholder="默认规则方案" /></el-form-item>
    <div class="policy-toolbar"><el-select v-model="selectedComponent" filterable aria-label="选择规则来源或自建条目" placeholder="选择固定来源版本或自建条目"><el-option v-for="row in choices" :key="row.key" :value="row.key" :label="row.label" :disabled="draft.components.some(item=>componentKey(item)===row.key)" /></el-select><el-button :disabled="!selectedComponent" @click="add">加入方案</el-button></div>
    <p v-if="options?.sources_truncated||options?.custom_truncated" class="inline-note">选择列表已达上限；未显示的来源请先在来源页核对，不能假定已全部加入。</p>
    <el-table :data="draft.components" class="spaced" empty-text="可保存空方案；不会自动加入所有来源。"><el-table-column type="index" label="顺序" width="60" /><el-table-column label="条目" min-width="260"><template #default="{row}">{{componentLabel(row)}}</template></el-table-column><el-table-column label="启用" width="90"><template #default="{row}"><el-switch v-model="row.enabled" :disabled="locked" /></template></el-table-column><el-table-column label="调整" min-width="200"><template #default="{row,$index}"><el-button text :disabled="locked||$index===0" @click="draft.components=moveComponent(draft.components,$index,-1)">上移</el-button><el-button text :disabled="locked||$index===draft.components.length-1" @click="draft.components=moveComponent(draft.components,$index,1)">下移</el-button><el-button text :disabled="locked" @click="remove($index)">移除</el-button></template></el-table-column></el-table>
    <h3>显式覆盖来源条目</h3><p class="small">来源原文不改写。可停用某条来源规则，或用相同匹配范围的启用自建规则替代；保护域校验仍有效。</p>
    <el-form-item label="来源条目"><el-select v-model="overrideTarget" filterable aria-label="要覆盖的来源条目"><el-option v-for="row in sourceKeys" :key="row.key" :value="row.key" :label="row.rule.value+' · '+actions[row.rule.action]+' · '+row.key" /></el-select></el-form-item>
    <el-form-item label="替代方式"><el-select v-model="overrideReplacement" aria-label="替代条目"><el-option value="" label="明确禁用这一条来源规则" /><el-option v-for="row in customKeys" :key="row.key" :value="row.key" :label="row.rule.value+' · '+actions[row.rule.action]" /></el-select></el-form-item><el-button :disabled="!overrideTarget" @click="setOverride">加入覆盖</el-button>
    <ul><li v-for="(replacement,target) in draft.overrides" :key="target" class="policy-override">{{target}} → {{replacement||'禁用'}} <el-button text :disabled="locked" @click="delete draft.overrides[target]">撤回覆盖</el-button></li></ul></el-form>
    <p class="small muted">保存会冻结所选来源与自建版本；并发修改须重新核对。HTTPS获取尚未开放，请先在来源页导入文件。</p>
    <el-alert v-if="error" :title="error" type="error" :closable="false" /><el-button v-if="editing&&error" :disabled="locked" @click="refreshEditRevision">读取当前修订号，保留输入</el-button>
    <template #footer><el-button :disabled="locked" @click="open=false">取消</el-button><el-button type="primary" :loading="busy" :disabled="busy||readOnly" @click="save">{{pending?'重试原请求':'保存方案版本'}}</el-button></template>
  </el-drawer>
</template>
<style scoped>
.policy-toolbar{display:flex;gap:16px;align-items:center;flex-wrap:wrap}.policy-toolbar>.el-input,.policy-toolbar>.el-select{flex:1;min-width:240px}.policy-json{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto}.policy-override{overflow-wrap:anywhere;margin:8px 0}.policy-toolbar h2{margin-right:auto}
</style>
