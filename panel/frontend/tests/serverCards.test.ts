import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse, compileScript } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import * as display from '../src/display.ts'
import { effectiveFreshness } from '../src/inventoryStatus.ts'

// 以下样本仅用于隔离测试，不是生产资产或实机采样。
const now=Date.parse('2026-10-17T12:00:00Z')
const observation={source:'测试固定采样',observed_at:'2026-10-17T11:59:00Z',expires_at:'2026-10-17T12:02:00Z',freshness:'fresh',scope:'测试机器观测',rate_state:'valid',metrics:{online:true,cpu_percent:18.5,cpu_window_seconds:1,memory_percent:40,disk_percent:30,network_rx_bytes:1000000000,network_tx_bytes:2000000000,network_rx_bytes_per_second:123000,network_tx_bytes_per_second:456000,network_window_seconds:60,network_interfaces:['test0']}}
const server=(id='sample-a')=>({id,name:'隔离测试服务器',enabled:true,notes:'',revision:1,adapter:'test',last_seen_at:observation.observed_at,ingress_count:1,egress_count:1,core_count:2,node_count:1,observation,monitoring:{state:'fresh',online:true,observed_at:observation.observed_at,expires_at:observation.expires_at,source:observation.source}})
const pagination={page:1,page_size:25,total:1,pages:1,has_next:false,has_previous:false}
const listing=(items:unknown[]=[server()])=>({items,pagination:{...pagination,total:items.length},read_only:true,limitations:[]})
const details=(id='sample-a')=>({server:server(id),observation,cores:{items:[],truncated:false},ingresses:{items:[],total:0,truncated:false},egresses:{items:[],total:0,truncated:false},nodes:{items:[],total:0,truncated:false},purchase_meter:null,checks:{items:[],truncated:false},capabilities:{edit:false,probe:false,manage_cores:false},check_actions:[],read_only:true,limitations:[]})
const source=readFileSync(new URL('../src/pages/AdminServersPage.vue',import.meta.url),'utf8')
function compile(text:string,inlineTemplate=false){return ts.transpileModule(compileScript(parse(text).descriptor,{id:'b5-test',inlineTemplate}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText}
const pageScript=compile(source)
const flush=async()=>{for(let i=0;i<20;i++)await Promise.resolve();await vue.nextTick()}
function deferred<T>(){let resolve!:(value:T)=>void,reject!:(e:unknown)=>void;const promise=new Promise<T>((a,b)=>{resolve=a;reject=b});return{promise,resolve,reject}}
function mount(send:(path:string,method?:string,body?:unknown,signal?:AbortSignal)=>Promise<any>,query:Record<string,string>={}){
  const scope=vue.effectScope(),ends:Array<()=>void>=[],exports:any={},route=vue.reactive({query}),replacements:any[]=[]
  const router={replace:async(next:any)=>{replacements.push(next);route.query=next.query}}
  const dependency=(name:string)=>name==='vue'?{...vue,onUnmounted:(fn:()=>void)=>ends.push(fn)}:name==='vue-router'?{useRoute:()=>route,useRouter:()=>router}:name==='../api'?{...api,request:send}:name==='../display'?display:name==='../inventoryStatus'?{useInventoryClock:()=>vue.ref(now)}:name.endsWith('.vue')?{}:undefined
  new Function('require','exports',pageScript)(dependency,exports)
  const state=scope.run(()=>exports.default.setup({},{expose:()=>{}}))
  return{state,route,replacements,ready:flush(),unmount(){ends.forEach(fn=>fn());scope.stop()}}
}
async function renderComponent(name:string,props:Record<string,unknown>){
  const text=readFileSync(new URL('../src/components/'+name+'.vue',import.meta.url),'utf8'),exports:any={}
  new Function('require','exports',compile(text,true))((dependency:string)=>dependency==='vue'?vue:dependency==='../display'?display:dependency==='../inventoryStatus'?{effectiveFreshness,useInventoryClock:()=>vue.ref(now)}:undefined,exports)
  const app=vue.createSSRApp(exports.default,props)
  app.component('ElTag',{setup:(_props:any,{slots}:any)=>()=>vue.h('span',slots.default?.())})
  return renderToString(app)
}

test('真实卡片组件含指标、来源和核心节点按钮，未知不补零',async()=>{
  const html=await renderComponent('ServerInventoryCard',{server:server(),now})
  for(const text of ['隔离测试服务器','18.5%','123 KB/s','456 KB/s','测试固定采样','最后联系','核心 2','节点 1','查看详情'])assert.ok(html.includes(text),text)
  assert.match(html,/<button[^>]*>核心 2/)
  const missing=await renderComponent('ServerInventoryCard',{server:{...server(),observation:null,core_count:undefined,node_count:undefined,monitoring:{state:'not_connected',online:null}},now,compact:true})
  assert.ok((missing.match(/未知/g)||[]).length>=7);assert.doesNotMatch(missing,/0%|0 B\/s/);assert.match(missing,/compact/)
})

test('卡片自动过期不伪造新采样，详情保留累计并明确套餐口径',async()=>{
  const html=await renderComponent('ServerInventoryCard',{server:server(),now:now+180000})
  assert.match(html,/采样已过期/);assert.doesNotMatch(html,/采样时在线/)
  const observed=await renderComponent('ServerObservationView',{observation})
  assert.match(observed,/123,000 B\/s/);assert.match(observed,/1 GB/);assert.match(observed,/不是个人套餐用量/)
  const unknown=await renderComponent('ServerObservationView',{observation:null})
  assert.match(unknown,/暂无可信样本/);assert.match(unknown,/固定主机采样/)
})

test('默认卡片五分区，采购图形、节点和检查实际组件均挂载',()=>{
  assert.match(source,/view = ref<'cards' \| 'compact'>\('cards'\)/)
  for(const section of ['overview','cores','nodes','usage','checks'])assert.ok(source.includes(`v-show="activeSection === '${section}'"`))
  for(const component of ['ServerInventoryCard','ServerCoreDetails','ServerObservationView','ResourceUsageCard','NodeResourceList','InventoryChecks','RefreshControl'])assert.ok(source.includes('<'+component))
  assert.match(source,/:meter="detail.purchase_meter"/);assert.match(source,/:nodes="detail.nodes.items"/);assert.match(source,/:actions="detail.check_actions \|\| \[\]"/)
})

test('刷新保留详情和分区，网络失败保留旧值与来源及读取时间',async()=>{
  let fail=false;const ui=mount(async path=>{if(fail)throw new api.ApiError(0,'NETWORK_ERROR','网络失败');return path.includes('?')?listing():details()})
  await ui.ready;await ui.state.showDetail(server());ui.state.activeSection.value='cores'
  const readAt=ui.state.lastReadAt.value,detailAt=ui.state.detailReadAt.value;fail=true;await ui.state.load();await ui.state.showDetail(server())
  assert.equal(ui.state.items.value.length,1);assert.equal(ui.state.selected.value.id,'sample-a');assert.equal(ui.state.activeSection.value,'cores')
  assert.equal(ui.state.detail.value.observation.observed_at,observation.observed_at);assert.equal(ui.state.lastReadAt.value,readAt);assert.equal(ui.state.detailReadAt.value,detailAt)
  assert.equal(ui.state.error.value,'网络失败');assert.equal(ui.state.detailError.value,'网络失败');ui.unmount()
})

test('同读取去重，筛选取消旧请求且迟到结果不能覆盖',async()=>{
  const jobs:Array<ReturnType<typeof deferred<any>>>=[],signals:AbortSignal[]=[]
  const ui=mount(async(_path,_method,_body,signal)=>{signals.push(signal!);const job=deferred<any>();jobs.push(job);return job.promise})
  await ui.ready;const duplicate=ui.state.load();assert.equal(jobs.length,1)
  ui.route.query={q:'新条件'};await flush();assert.equal(jobs.length,2);assert.equal(signals[0].aborted,true)
  jobs[1].resolve(listing([server('new')]));await flush();jobs[0].resolve(listing([server('old')]));await duplicate;assert.equal(ui.state.items.value[0].id,'new')
  const one=ui.state.showDetail(server('new')),two=ui.state.showDetail(server('new'));assert.equal(jobs.length,3)
  jobs[2].resolve(details('new'));await Promise.all([one,two]);ui.unmount()
})

test('快速切详情、关闭和卸载隔离迟到结果',async()=>{
  const jobs:Array<ReturnType<typeof deferred<any>>>=[]
  const ui=mount(async path=>{if(path.includes('?'))return listing();const job=deferred<any>();jobs.push(job);return job.promise});await ui.ready
  const first=ui.state.showDetail(server('one')),second=ui.state.showDetail(server('two'));jobs[1].resolve(details('two'));await second;jobs[0].resolve(details('one'));await first;assert.equal(ui.state.detail.value.server.id,'two')
  const closed=ui.state.showDetail(server('three'));ui.state.closeDetail();jobs[2].resolve(details('three'));await closed;assert.equal(ui.state.detail.value,null)
  const gone=ui.state.showDetail(server('four'));ui.unmount();jobs[3].resolve(details('four'));await gone;assert.equal(ui.state.detail.value,null)
})

test('深链直接读取指定对象分区，切区不重读列表，关闭移除对象query',async()=>{
  const paths:string[]=[],ui=mount(async path=>{paths.push(path);return path.includes('?')?listing():details('deep')},{server_id:'deep',section:'nodes',status:'disabled'})
  await ui.ready;assert.equal(ui.state.selected.value.id,'deep');assert.equal(ui.state.activeSection.value,'nodes');assert.ok(paths.includes('/admin/servers/deep'))
  const count=paths.length;ui.state.selectSection('usage');await flush();assert.equal(paths.length,count);assert.equal(ui.route.query.section,'usage');assert.equal(ui.route.query.status,'disabled')
  ui.state.closeDrawer();await flush();assert.equal(ui.route.query.server_id,undefined);assert.equal(ui.state.selected.value,null);ui.unmount()
})

test('403撤除旧资产与详情，404详情不能保留可操作对象',async()=>{
  let failure=0;const ui=mount(async path=>{if(failure)throw new api.ApiError(failure,'denied','不可访问');return path.includes('?')?listing():details()})
  await ui.ready;await ui.state.showDetail(server());failure=404;await ui.state.showDetail(server());assert.equal(ui.state.detail.value,null);assert.equal(ui.state.detailReadAt.value,null)
  failure=403;await ui.state.load();assert.deepEqual(ui.state.items.value,[]);assert.equal(ui.state.selected.value,null);assert.equal(ui.state.lastReadAt.value,null);ui.unmount()
})

test('详情正在刷新时保留同对象内容；成功后回填新样本',async()=>{
  let job:ReturnType<typeof deferred<any>>|null=null
  const ui=mount(async path=>path.includes('?')?listing():job?job.promise:details())
  await ui.ready;await ui.state.showDetail(server());ui.state.activeSection.value='nodes'
  job=deferred<any>();const reading=ui.state.showDetail(server())
  assert.equal(ui.state.detailBusy.value,true);assert.equal(ui.state.detail.value.server.id,'sample-a');assert.equal(ui.state.activeSection.value,'nodes')
  job.resolve({...details(),observation:{...observation,observed_at:'2026-10-17T12:00:00Z'}});await reading
  assert.equal(ui.state.detail.value.observation.observed_at,'2026-10-17T12:00:00Z');assert.equal(ui.state.detailBusy.value,false);ui.unmount()
})

test('核心资料由真实CoreMetadata选中，刷新保选中并撤除已删除对象',async()=>{
  const coreSource=readFileSync(new URL('../src/components/ServerCoreDetails.vue',import.meta.url),'utf8'),exports:any={}
  new Function('require','exports',compile(coreSource))((name:string)=>name==='vue'?vue:name==='../display'?display:undefined,exports)
  const core={id:'core-test',name:'测试核心',core_type:'xray',instance_key:'test',registered_version:'test-version',configuration_owner:'test-owner',configuration_version:'rev-1',revision:1,notes:'',ingress_ids:['1'],association_valid:true,service_unit:'test.service',owner_state:'observed_only',evidence_source:'测试清单',evidence_observed_at:observation.observed_at}
  const props=vue.reactive({cores:{items:[core],truncated:false}}),scope=vue.effectScope()
  const state=scope.run(()=>exports.default.setup(props,{expose:()=>{}}))
  assert.equal(state.selected.value,null);state.selectedId.value=core.id
  assert.equal(state.selected.value.service_unit,'test.service');assert.equal(state.selected.value.configuration_owner,'test-owner')
  props.cores={items:[{...core,registered_version:'new-test-version'}],truncated:false};await flush()
  assert.equal(state.selectedId.value,core.id);assert.equal(state.selected.value.registered_version,'new-test-version')
  props.cores={items:[],truncated:false};await flush();assert.equal(state.selected.value,null)
  assert.doesNotMatch(coreSource,/@click="(?:restart|upgrade|apply)/);scope.stop()
})

test('采购来源失败仅在相同已授权绑定保留旧圆环数据，解绑和撤权立即清除',async()=>{
  const meter={id:'meter-test',label:'测试采购',scope:'provider',source_kind:'official',quality:'current',used_bytes:'100',quota_bytes:'1000',remaining_bytes:'900',upload_bytes:null,download_bytes:null,observed_at:observation.observed_at,expires_at:observation.expires_at,cycle:null,expires_on:null,alerts:[],details:{}}
  let value:any={...details(),purchase_meter:meter,purchase_binding_key:'authorized-source:meter-test',purchase_meter_state:'ready'}
  const ui=mount(async path=>path.includes('?')?listing():value);await ui.ready;await ui.state.showDetail(server())
  value={...value,purchase_meter:null,purchase_meter_state:'unavailable'};await ui.state.showDetail(server())
  assert.equal(ui.state.detail.value.purchase_meter.used_bytes,'100');assert.equal(ui.state.detail.value.purchase_meter.quality,'error');assert.equal(ui.state.detail.value.purchase_meter.observed_at,observation.observed_at)
  await ui.state.showDetail(server());assert.equal(ui.state.detail.value.purchase_meter.alerts.length,1)
  value={...value,purchase_binding_key:null,purchase_meter_state:'forbidden'};await ui.state.showDetail(server());assert.equal(ui.state.detail.value.purchase_meter,null)
  value={...value,purchase_binding_key:'different',purchase_meter_state:'unavailable'};await ui.state.showDetail(server());assert.equal(ui.state.detail.value.purchase_meter,null);ui.unmount()
})

test('自动读取只读取现有接口，页面隐藏时暂停，不改来源采样时间',async()=>{
  const calls:string[]=[],ui=mount(async path=>{calls.push(path);return path.includes('?')?listing():details()});await ui.ready
  ui.state.refreshObservedData();await flush();assert.equal(calls.length,2);assert.equal(ui.state.items.value[0].observation.observed_at,observation.observed_at)
  const original=Object.getOwnPropertyDescriptor(globalThis,'document')
  try{Object.defineProperty(globalThis,'document',{value:{visibilityState:'hidden'},configurable:true});ui.state.refreshObservedData();await flush();assert.equal(calls.length,2)}finally{if(original)Object.defineProperty(globalThis,'document',original);else Reflect.deleteProperty(globalThis,'document');ui.unmount()}
  assert.ok(calls.every(path=>path.startsWith('/admin/servers?')))
})
