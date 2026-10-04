<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { auth, hydrateSession } from '../auth'
import { request, errorMessage } from '../api'
import { boundedRequest } from '../billingPending'
import { useSourceImport, type RuleSource, type SourceRule } from '../sourceImport'
import type { Pagination } from '../types'
interface Listing { items:RuleSource[]; pagination:Pagination; read_only:boolean; message:string }
interface Detail { item:RuleSource; version:{revision:number;sha256:string;count:number}; history:{revision:number;count:number;created_at:string}[]; history_truncated:boolean; items:SourceRule[]; pagination:Pagination }
const verified=ref(false),identity=computed(()=>verified.value&&auth.session?.authenticated&&auth.session.user?.is_staff?auth.session.user.username:'')
const listing=ref<Listing|null>(null),error=ref(''),loading=ref(false),q=ref(''),page=ref(1)
const detail=ref<Detail|null>(null),detailOpen=ref(false),detailError=ref(''),detailBusy=ref(false),detailPage=ref(1),detailVersion=ref(0),detailId=ref('')
let reads=0,details=0
async function load(){
  if(!identity.value)return
  const token=++reads,key=identity.value;loading.value=true;error.value=''
  try{const data=await boundedRequest(request<Listing>('/admin/rule-sources?'+new URLSearchParams({q:q.value,page:String(page.value),page_size:'25'})));if(token===reads&&key===identity.value)listing.value=data}
  catch(e){if(token===reads&&key===identity.value)error.value=errorMessage(e)}finally{if(token===reads&&key===identity.value)loading.value=false}
}
const editor=useSourceImport(identity,load)
const {draft,open,busy,fileBusy,error:editError,notice,preview,fileName,revisionConflict,outstanding}=editor
async function showDetail(source:RuleSource){detailId.value=source.source_id;detailPage.value=1;detailVersion.value=source.revision;detail.value=null;detailOpen.value=true;await loadDetail()}
async function loadDetail(){
  if(!identity.value||!detailId.value)return
  const token=++details,key=identity.value;detailBusy.value=true;detailError.value=''
  try{const data=await boundedRequest(request<Detail>('/admin/rule-sources/'+encodeURIComponent(detailId.value)+'?'+new URLSearchParams({version:String(detailVersion.value),page:String(detailPage.value),page_size:'25'})));if(token===details&&key===identity.value)detail.value=data}
  catch(e){if(token===details&&key===identity.value)detailError.value=errorMessage(e)}finally{if(token===details&&key===identity.value)detailBusy.value=false}
}
function changeVersion(){detailPage.value=1;void loadDetail()}
function search(){page.value=1;void load()}
async function selectFile(event:Event){const input=event.target as HTMLInputElement;if(input.files?.[0])await editor.readFile(input.files[0]);input.value=''}
const changes=computed(()=>preview.value?[
  ...preview.value.diff.added.map(row=>({...row,change:'新增'})),
  ...preview.value.diff.removed.map(row=>({...row,change:'移除'})),
  ...preview.value.diff.modified.map(row=>({...row.after,change:'修改',before:row.before})),
]:[])
const actions:Record<string,string>={client_direct:'本机直连',proxy:'当前代理'}
const kinds:Record<string,string>={exact:'精确域名',suffix:'域名及子域名',regex:'限定正则'}
watch(identity,()=>{reads++;details++;listing.value=null;detail.value=null;detailOpen.value=false;loading.value=false;detailBusy.value=false;error.value='';if(identity.value)void load()})
onMounted(async()=>{try{await hydrateSession(true);verified.value=true}catch(e){error.value=errorMessage(e)}})
onUnmounted(()=>{reads++;details++;editor.dispose()})
</script>
<template>
  <div class="page-title"><div><p class="eyebrow">管理员工作区 · 代理规则</p><h1>规则来源</h1><p class="muted">导入一份来源，先核对差异，再保存独立版本。</p></div><el-button type="primary" :disabled="!listing||listing.read_only||!!outstanding||busy" @click="editor.begin()">导入规则文件</el-button></div>
  <nav class="source-navigation" aria-label="规则分区"><RouterLink to="/admin/rules">自建规则与命中</RouterLink><strong>规则来源</strong><RouterLink to="/admin/rule-policies">规则方案</RouterLink></nav>
  <el-alert v-if="notice" :title="notice" type="success" :closable="false" class="spaced" />
  <el-alert v-if="outstanding&&!open" title="上次保存结果尚未确认。" type="warning" :closable="false"><el-button @click="open=true">继续确认</el-button></el-alert>
  <section class="surface admin-panel">
    <p class="inline-note">来源与自建规则分别保留。保存新版本不会自动改动已有方案；请在规则方案中选择明确版本、核对覆盖与顺序，再生成发布检查材料。服务器与客户端不会因本页保存而改变。</p>
    <form class="table-filters" @submit.prevent="search"><el-input v-model="q" aria-label="搜索来源" placeholder="搜索来源名称" maxlength="100" clearable /><el-button native-type="submit" :loading="loading">搜索</el-button><el-button :loading="loading" @click="load">刷新</el-button></form>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-table v-else v-loading="loading" :data="listing?.items||[]" row-key="source_id" empty-text="尚未导入规则来源；原有自建规则仍在另一个分区。">
      <el-table-column label="来源" min-width="200"><template #default="{row}"><el-button link type="primary" @click="showDetail(row)">{{row.name}}</el-button></template></el-table-column>
      <el-table-column label="当前版本" width="100"><template #default="{row}">v{{row.revision}}</template></el-table-column><el-table-column label="规则数" prop="count" width="100" />
      <el-table-column label="状态" min-width="150"><template #default><el-tag type="info">待组合候选</el-tag></template></el-table-column>
      <el-table-column label="操作" min-width="200"><template #default="{row}"><el-button text @click="showDetail(row)">查看版本</el-button><el-button text :disabled="listing?.read_only||!!outstanding||busy" @click="editor.begin(row)">更新文件</el-button></template></el-table-column>
    </el-table>
    <el-pagination v-if="listing&&listing.pagination.pages>1" v-model:current-page="page" :page-size="25" :total="listing.pagination.total" layout="prev, pager, next" @current-change="load" />
  </section>
  <el-drawer :model-value="open" :title="draft.source_id?'更新规则来源':'导入规则来源'" size="min(800px, 100vw)" :close-on-click-modal="false" :close-on-press-escape="!busy&&!outstanding" :show-close="!busy&&!outstanding" @close="editor.close">
    <el-alert v-if="editError" :title="editError" type="error" :closable="false" class="spaced" />
    <el-button v-if="revisionConflict&&draft.source_id" :disabled="busy||!!outstanding" @click="editor.refreshRevision">读取来源当前版本，保留输入</el-button>
    <el-form label-position="top" :disabled="busy||fileBusy||!!outstanding">
      <el-form-item label="来源名称"><el-input v-model="draft.name" aria-label="来源名称" placeholder="例如：工作用域名规则" maxlength="100" /></el-form-item>
      <el-form-item label="规则文件"><input type="file" accept=".json,application/json" aria-label="上传规则文件" :disabled="busy||!!outstanding" @change="selectFile" /><span v-if="fileName" class="small">{{fileName}}</span></el-form-item>
      <p class="small muted">UTF-8 JSON，最多256 KiB、500条。也可粘贴下方规则内容；文件不会自动保存或发布。</p>
      <el-form-item label="文件内容"><el-input v-model="draft.document" type="textarea" :rows="7" aria-label="规则文件内容" placeholder="上传文件后在这里核对，也可以直接粘贴JSON。" /></el-form-item>
    </el-form>
    <el-collapse class="spaced"><el-collapse-item title="支持的格式与示例" name="format"><p>仅接受以下明确版本的域名规则；不直接接受完整客户端配置、YAML或编译后的规则集。不支持的字段会被拒绝。</p><pre class="source-example">{{JSON.stringify({schema_version:1,rules:[{action:'proxy',kind:'suffix',value:'example.com',enabled:true}]},null,2)}}</pre><p class="small">action可为proxy或client_direct；kind可为exact、suffix、regex。正则必须有scope_domain限定范围。保护域不能被改为直连。</p></el-collapse-item></el-collapse>
    <section v-if="preview" class="source-preview" aria-label="导入差异预览">
      <h2>核对差异</h2><p>导入后 {{preview.count}} 条 · 新增 {{preview.diff.added.length}} · 移除 {{preview.diff.removed.length}} · 修改 {{preview.diff.modified.length}}</p>
      <p v-if="preview.diff.order_changed" class="inline-note">文件内条目顺序有变化。此次只保存来源顺序，不改变当前匹配优先级。</p>
      <p class="small muted">“移除”只影响这一来源的新版本；历史版本和自建规则都会保留。</p>
      <el-table :data="changes" max-height="280" empty-text="规则内容没有变化"><el-table-column label="变化" prop="change" width="80" /><el-table-column label="匹配" min-width="240"><template #default="{row}">{{kinds[row.kind]}} · {{row.value}}<div v-if="row.before" class="small muted">原动作 {{actions[row.before.action]}} · {{row.before.enabled?'启用':'停用'}}</div></template></el-table-column><el-table-column label="动作" min-width="130"><template #default="{row}">{{actions[row.action]}} · {{row.enabled?'启用':'停用'}}</template></el-table-column></el-table>
      <div v-if="preview.conflicts.length" class="inline-note"><h3>重叠与冲突提示</h3><p class="small">来源尚未组合；这里不决定最终优先级。后续方案绑定前需要处理这些重叠。</p><ul><li v-for="(item,i) in preview.conflicts" :key="i"><strong v-if="item.candidate_value">{{item.candidate_value}}（{{actions[item.candidate_action||'']}}） ↔ {{item.other_value}}（{{actions[item.other_action||'']}}）</strong><div>{{item.source_name?item.source_name+'：':''}}{{item.message}}</div></li></ul><p v-if="preview.conflicts_truncated">仅显示前100项提示，完整组合仍需后续审核。</p></div>
      <p class="small">{{preview.message}}</p>
    </section>
    <template #footer><el-button :disabled="busy||!!outstanding" @click="editor.close">取消</el-button><el-button :loading="busy&&!outstanding" :disabled="busy||fileBusy||!!outstanding||!draft.name.trim()||!draft.document.trim()" @click="editor.inspect">预览差异</el-button><el-button type="primary" :loading="busy&&!!outstanding" :disabled="busy||fileBusy||(!outstanding&&!preview?.can_commit)" @click="editor.save">{{outstanding?'重试原请求':'确认保存候选版本'}}</el-button></template>
  </el-drawer>
  <el-drawer v-model="detailOpen" title="来源版本" size="min(850px, 100vw)">
    <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" />
    <el-skeleton v-if="detailBusy&&!detail" :rows="5" animated />
    <template v-if="detail"><h2>{{detail.item.name}}</h2><p>待组合候选 · 当前 v{{detail.item.revision}} · 查阅 v{{detail.version.revision}}</p>
      <el-select v-model="detailVersion" aria-label="来源历史版本" :disabled="detailBusy" @change="changeVersion"><el-option v-for="version in detail.history" :key="version.revision" :value="version.revision" :label="'v'+version.revision+' · '+version.count+'条'" /></el-select><p v-if="detail.history_truncated" class="small">仅列最近100个版本。</p>
      <p class="small muted">此处只查看来源内容，不表示已加入当前匹配或客户端配置。</p>
      <el-table v-loading="detailBusy" :data="detail.items"><el-table-column label="规则" prop="value" min-width="240" /><el-table-column label="匹配类型" min-width="140"><template #default="{row}">{{kinds[row.kind]}}</template></el-table-column><el-table-column label="动作" min-width="120"><template #default="{row}">{{actions[row.action]}}</template></el-table-column><el-table-column label="登记状态" width="100"><template #default="{row}">{{row.enabled?'启用':'停用'}}</template></el-table-column></el-table>
      <el-pagination v-if="detail.pagination.pages>1" v-model:current-page="detailPage" :page-size="25" :total="detail.pagination.total" layout="prev, pager, next" @current-change="loadDetail" />
    </template>
  </el-drawer>
</template>
<style scoped>
.source-navigation{display:flex;gap:24px;margin:0 0 20px}.source-example{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6fa;padding:14px;border-radius:8px}.source-preview{margin-top:24px}.source-preview li{margin:8px 0}.source-preview h3{font-size:16px}input[type=file]{max-width:100%}
</style>
