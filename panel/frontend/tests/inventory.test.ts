import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'
import * as display from '../src/display.ts'
import { effectiveFreshness } from '../src/inventoryStatus.ts'

function deferred<T>() {
  let resolve!:(value:T)=>void, reject!:(reason:unknown)=>void
  const promise = new Promise<T>((yes,no)=>{resolve=yes;reject=no})
  return {promise,resolve,reject}
}
const flush = async()=>{for(let i=0;i<15;i++)await Promise.resolve();await vue.nextTick()}
const pagination = {page:1,page_size:25,total:1,pages:1,has_next:false,has_previous:false}
const server = (id='server-a')=>({id,name:'登记机器',enabled:true,adapter:'xray',last_seen_at:'2026-10-04T00:00:00Z',ingress_count:1,egress_count:1,monitoring:{state:'not_connected',online:null,observed_at:null}})
const listing = (items:unknown[])=>({items,pagination:{...pagination,total:items.length},read_only:true,limitations:[]})
const serverDetail = (id='server-a')=>({server:server(id),metrics:{cpu_percent:null,memory_percent:null,disk_percent:null,upload_bps:null,download_bps:null,total_transfer_bytes:null},core:{actual_version:null,state:'not_connected'},ingresses:{items:[],total:0,truncated:false},egresses:{items:[],total:0,truncated:false},capabilities:{edit:false,probe:false,manage_cores:false},read_only:true,limitations:[]})
const line = (id='line-a')=>({id,name:'登记线路',enabled:true,ingresses:[],ingress_count:0,ingresses_truncated:false,egress:{id:'1',name:'登记出口',kind:'upstream',kind_label:'上游链路',fail_closed:true,server:{id:'server-a',name:'登记机器',enabled:true}},endpoint_registration:'enabled',verification:{state:'not_tested',observed_at:null}})
const lineDetail = (id='line-a')=>({line:line(id),read_only:true,capabilities:{edit:false,probe:false,publish:false},limitations:[]})
const require = createRequire(import.meta.url)
const sources = ['AdminServersPage','AdminLinesPage'].map(name=>parse(readFileSync(new URL('../src/pages/'+name+'.vue',import.meta.url),'utf8')).descriptor)
const scripts = sources.map((descriptor,index)=>ts.transpileModule(compileScript(descriptor,{id:'inventory-'+index}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)

// 执行真实页面 setup，仅替换网络和路由宿主；保持 Vue 的响应式与 watch。
function mount(kind:'servers'|'lines',send:(path:string,method?:string)=>Promise<unknown>,query:Record<string,string>={}) {
  const ends:Array<()=>void>=[],scope=vue.effectScope()
  const route=vue.reactive({query}),replacements:Array<{path:string;query:Record<string,string>}> = []
  const router={replace:async(next:{path:string;query:Record<string,string>})=>{replacements.push(next);route.query=next.query}}
  const exports:Record<string,any>={}
  const moduleRequire=(name:string)=>name==='vue'?{...vue,onUnmounted:(fn:()=>void)=>ends.push(fn)}:
    name==='vue-router'?{useRoute:()=>route,useRouter:()=>router}:name==='../api'?{...api,request:send}:name==='../display'?display:
      name==='../inventoryStatus'?{effectiveFreshness,useInventoryClock:()=>vue.ref(Date.now())}:name.endsWith('.vue')?{}:require(name)
  new Function('require','exports',scripts[kind==='servers'?0:1])(moduleRequire,exports)
  const state=scope.run(()=>exports.default.setup({},{expose:()=>{}}))
  return {state,route,replacements,ready:flush(),unmount(){ends.forEach(fn=>fn());scope.stop()}}
}

test('服务器列表读取登记并按真实详情请求；最近记录不能推断在线',async()=>{
  const paths:string[]=[]
  const ui=mount('servers',async(path,method)=>{assert.equal(method,undefined);paths.push(path);return path.includes('/server-a')?serverDetail():listing([server()])})
  await ui.ready
  assert.equal(ui.state.items.value[0].monitoring.online,null)
  assert.equal(new URLSearchParams(paths[0].split('?')[1]).get('page_size'),'25')
  await ui.state.showDetail(ui.state.items.value[0])
  assert.equal(paths[1],'/admin/servers/server-a')
  assert.equal(ui.state.detail.value.core.actual_version,null)
  assert.equal(ui.state.recordedAt(null),'暂无记录')
  ui.unmount()
})

test('搜索重置页码，分页保留登记筛选，浏览器路由恢复有效查询',async()=>{
  const paths:string[]=[]
  const ui=mount('servers',async(path)=>{paths.push(path);return listing([])},{q:'旧搜索',status:'disabled',page:'3'})
  await ui.ready
  assert.equal(ui.state.page.value,3)
  ui.state.q.value='  新搜索  '
  await ui.state.updateFilters(1);await flush()
  assert.deepEqual(ui.replacements[0],{path:'/admin/servers',query:{q:'新搜索',status:'disabled'}})
  let params=new URLSearchParams(paths.at(-1)!.split('?')[1])
  assert.equal(params.get('q'),'新搜索');assert.equal(params.get('page'),'1')
  await ui.state.updateFilters(2);await flush()
  params=new URLSearchParams(paths.at(-1)!.split('?')[1])
  assert.equal(params.get('status'),'disabled');assert.equal(params.get('page'),'2')
  ui.route.query={status:'online',page:'-1'};await flush()
  assert.equal(ui.state.status.value,'all');assert.equal(ui.state.page.value,1)
  ui.unmount()
})

test('线路精确关联服务器筛选可恢复和清除，并保留其他筛选',async()=>{
  const paths:string[]=[]
  const ui=mount('lines',async(path)=>{paths.push(path);return listing([line()])},{server_id:'server-a',q:'入口'})
  await ui.ready
  let params=new URLSearchParams(paths.at(-1)!.split('?')[1])
  assert.equal(params.get('server_id'),'server-a')
  await ui.state.updateFilters(2);await flush()
  assert.equal(ui.replacements[0].query.server_id,'server-a')
  ui.state.clearServer();await flush()
  params=new URLSearchParams(paths.at(-1)!.split('?')[1])
  assert.equal(params.get('server_id'),null);assert.equal(params.get('q'),'入口');assert.equal(params.get('page'),'1')
  ui.unmount()
})

test('不同筛选并发时旧列表返回不会覆盖当前页，卸载后不接收请求',async()=>{
  for(const kind of ['servers','lines'] as const){
    const requests:Array<ReturnType<typeof deferred<unknown>>>=[]
    const ui=mount(kind,async()=>{const current=deferred<unknown>();requests.push(current);return current.promise})
    await ui.ready
    ui.route.query={q:'新筛选'};await flush()
    requests[1].resolve(listing([{id:'current'}]));await flush()
    requests[0].resolve(listing([{id:'stale'}]));await flush()
    assert.equal(ui.state.items.value[0].id,'current')
    const pending=ui.state.load();await flush();ui.unmount()
    requests[2].resolve(listing([{id:'after-unmount'}]));await pending
    assert.equal(ui.state.items.value[0].id,'current')
  }
})

test('快速切换详情、关闭与重新筛选均隔离迟到的旧详情',async()=>{
  for(const kind of ['servers','lines'] as const){
    const requests:Array<ReturnType<typeof deferred<unknown>>>=[]
    const ui=mount(kind,async(path)=>{if(path.includes('?'))return listing([]);const current=deferred<unknown>();requests.push(current);return current.promise})
    await ui.ready
    const row=kind==='servers'?server:line,project=kind==='servers'?serverDetail:lineDetail
    const first=ui.state.showDetail(row('first')),second=ui.state.showDetail(row('second'))
    requests[1].resolve(project('second'));await second
    requests[0].resolve(project('first'));await first
    assert.equal(ui.state.detail.value[kind==='servers'?'server':'line'].id,'second')
    const closed=ui.state.showDetail(row('closed'));ui.state.closeDetail()
    requests[2].resolve(project('closed'));await closed
    assert.equal(ui.state.selected.value,null);assert.equal(ui.state.detail.value,null)
    const filtered=ui.state.showDetail(row('before-filter'))
    ui.route.query={q:'改变筛选'};await flush()
    requests[3].resolve(project('before-filter'));await filtered
    assert.equal(ui.state.detail.value,null)
    ui.unmount()
  }
})

test('空列表与请求失败保留准确状态，详情错误可重试',async()=>{
  for(const kind of ['servers','lines'] as const){
    let failList=false,failDetail=true
    const ui=mount(kind,async(path)=>{if(path.includes('?')){if(failList)throw new api.ApiError(403,'permission_denied','此操作仅限管理员。');return listing([])}
      if(failDetail)throw new api.ApiError(404,'not_found','资源不存在或不可访问。');return kind==='servers'?serverDetail():lineDetail()})
    await ui.ready
    assert.deepEqual(ui.state.items.value,[]);assert.equal(ui.state.error.value,'')
    const row=kind==='servers'?server():line()
    await ui.state.showDetail(row)
    assert.equal(ui.state.detail.value,null);assert.equal(ui.state.detailError.value,'资源不存在或不可访问。')
    failDetail=false;await ui.state.showDetail(row)
    assert.ok(ui.state.detail.value);assert.equal(ui.state.detailError.value,'')
    failList=true;await ui.state.load()
    assert.equal(ui.state.error.value,'此操作仅限管理员。');assert.equal(ui.state.busy.value,false)
    assert.equal(ui.state.detail.value,null)
    ui.unmount()
  }
})

test('模板明确登记与监控的范围，未提供伪造探针或绿色健康状态',()=>{
  const servers=sources[0].template!.content,lines=sources[1].template!.content
  const observation=readFileSync(new URL('../src/components/ServerObservationView.vue',import.meta.url),'utf8')
  assert.ok(servers.includes('最后记录'));assert.ok(observation.includes('未接入监控'));assert.ok(observation.includes('暂无可信样本'))
  assert.ok(lines.includes('入口服务器 / 协议'));assert.ok(lines.includes('出口服务器 / 类型'));assert.ok(lines.includes('完整上下游编排'))
  for(const template of [servers,lines]){
    assert.ok(template.includes('登记启用'))
    assert.ok(!template.includes('type="success"'))
    assert.ok(!template.includes('@click="probe'))
  }
})
