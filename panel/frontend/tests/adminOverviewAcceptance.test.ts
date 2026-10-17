import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'
import * as api from '../src/api.ts'

// 隔离假数据，仅替换网络与Element宿主；实际页面、采购图和刷新逻辑均真实挂载。
const root=fileURLToPath(new URL('../src/',import.meta.url)),require=createRequire(import.meta.url)
const overview=(total=23)=>({users:{total,active:20,disabled:3},services:{total:31,expired:2,mapping_required:1,recorded_metering_gaps:0,statistics_incomplete:null},generated_at:'2026-10-17T13:00:00Z',environment:{kind:'production',is_demo:false},limitations:['INTERNAL_LIMITATION_SHOULD_NOT_RENDER']})
const meter=(id='home')=>({id,label:id==='home'?'HOME测试采购':'BWH测试采购',scope:id==='home'?'external_node':'server',source_kind:id==='home'?'estimate':'official',quality:'current',quota_bytes:'400000000000',used_bytes:'73000000000',remaining_bytes:'327000000000',upload_bytes:'23000000000',download_bytes:'50000000000',observed_at:'2026-10-17T13:00:00Z',expires_at:'2099-10-17T13:03:00Z',cycle:{kind:'fixed_days',starts_at:'2026-09-19T16:00:00Z',ends_at:'2026-10-19T16:00:00Z',next_reset_at:'2026-10-19T16:00:00Z'},expires_on:'2027-01-19',alerts:[],details:{}})
const resources=()=>({items:[{service_id:'authorized-personal-fixture',provider_usage:{schema_version:2,source_key:'same-authorized-source',generated_at:'2026-10-17T13:00:00Z',meters:[meter(),meter('bwg')]}}]})
function deferred<T>(){let resolve!:(value:T)=>void,reject!:(value:unknown)=>void;const promise=new Promise<T>((a,b)=>{resolve=a;reject=b});return{promise,resolve,reject}}
const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();await vue.nextTick()}
type Node={tag:string;text:string;props:Record<string,any>;children:Node[];parent:Node|null}
const node=(tag:string,text=''):Node=>({tag,text,props:{},children:[],parent:null})
function walk(item:Node):Node[]{return[item,...item.children.flatMap(walk)]}
function textOf(item:Node):string{return item.text+item.children.map(textOf).join('')}
function classHas(item:Node,name:string){return String(item.props.class||'').split(' ').includes(name)}
function mount(send:(path:string,signal?:AbortSignal)=>Promise<unknown>,target='pages/AdminOverviewPage.vue',props:Record<string,unknown>={}){
  const cache=new Map<string,any>(),calls:string[]=[],auth=vue.reactive({session:{authenticated:true,user:{username:'acceptance-admin',is_staff:true},csrf_token:'fake-test-session'}})
  function load(filename:string):any{
    if(cache.has(filename))return cache.get(filename)
    let content=readFileSync(filename,'utf8')
    if(filename.endsWith('.vue'))content=compileScript(parse(content).descriptor,{id:filename,inlineTemplate:true}).content
    const code=ts.transpileModule(content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
    const exports:any={};cache.set(filename,exports)
    new Function('require','exports',code)((name:string)=>{
      if(name==='vue')return vue
      if(/\/auth(?:\.ts)?$/.test(name))return{auth}
      if(/\/api(?:\.ts)?$/.test(name))return{...api,request:async(path:string,_method?:string,_body?:unknown,signal?:AbortSignal)=>{calls.push(path);return send(path,signal)}}
      if(!name.startsWith('.'))return require(name)
      return load(resolve(dirname(filename),/\.(vue|ts)$/.test(name)?name:name+'.ts'))
    },exports)
    return exports
  }
  const renderer=vue.createRenderer<Node,Node>({
    createElement:tag=>node(tag),createText:text=>node('#text',text),createComment:text=>node('#comment',text),
    insert:(child,parent,anchor)=>{if(child.parent){const old=child.parent.children.indexOf(child);if(old>=0)child.parent.children.splice(old,1)}child.parent=parent;const index=anchor?parent.children.indexOf(anchor):-1;if(index<0)parent.children.push(child);else parent.children.splice(index,0,child)},
    remove:child=>{if(child.parent){const index=child.parent.children.indexOf(child);if(index>=0)child.parent.children.splice(index,1)}child.parent=null},
    setText:(item,value)=>{item.text=value},setElementText:(item,value)=>{item.text=value;item.children=[]},parentNode:item=>item.parent,
    nextSibling:item=>item.parent?.children[item.parent.children.indexOf(item)+1]||null,
    patchProp:(item,key,_before,after)=>{item.props[key]=after},
  })
  const component=load(resolve(root,target)).default,app=renderer.createApp(component,props)
  app.component('ElButton',{props:['disabled'],setup:(p:any,{attrs,slots}:any)=>()=>vue.h('button',{...attrs,disabled:p.disabled},slots.default?.())})
  app.component('ElAlert',{props:['title'],setup:(p:any)=>()=>vue.h('p',{role:'alert'},p.title)})
  app.component('ElSkeleton',{setup:()=>()=>vue.h('div',{'data-skeleton':true})})
  app.component('RouterLink',{props:['to'],setup:(p:any,{slots}:any)=>()=>vue.h('a',{href:p.to},slots.default?.())})
  const host=node('root');app.mount(host)
  return{host,calls,auth,ready:flush(),nodes:()=>walk(host),text:()=>textOf(host),refresh:()=>walk(host).find(item=>item.tag==='button'&&classHas(item,'refresh-button'))!,unmount:()=>app.unmount()}
}

test('总览真实组件只有一个刷新入口，保采购图并给本人服务入口，不加载旧快照',async()=>{
  const ui=mount(async path=>path==='/admin/overview'?overview():resources())
  try{
    await ui.ready
    assert.deepEqual(ui.calls.sort(),['/admin/overview','/admin/resource-usage'])
    assert.equal(ui.nodes().filter(item=>classHas(item,'refresh-control')).length,1)
    assert.equal(ui.nodes().filter(item=>item.props['data-chart']==='quota-gauge').length,2)
    assert.equal(ui.nodes().filter(item=>item.props['data-chart']==='transfer-composition').length,2)
    const link=ui.nodes().find(item=>item.tag==='a'&&item.props.href==='/services')!
    assert.ok(link);assert.match(ui.text(),/我的套餐与流量/);assert.match(ui.text(),/近7天、本月、今年/)
    assert.match(ui.text(),/HOME测试采购/);assert.match(ui.text(),/资源采购用量/)
    assert.doesNotMatch(ui.text(),/尚未汇总|本地候选统计|INTERNAL_LIMITATION|当前数据范围|计量质量汇总|待核对计量/)
    assert.ok(!ui.calls.some(path=>/observation|aggregate|\/me\//.test(path)))
  }finally{ui.unmount()}
})

test('一个按钮同时刷新两份数据，重复点击去重且刷新中保留原图和数字',async()=>{
  let pending=false
  const jobs=new Map<string,ReturnType<typeof deferred<unknown>>>()
  const ui=mount(async path=>{if(!pending)return path==='/admin/overview'?overview():resources();const job=deferred<unknown>();jobs.set(path,job);return job.promise})
  try{
    await ui.ready;pending=true
    ui.refresh().props.onClick();ui.refresh().props.onClick();await flush()
    assert.equal(ui.calls.filter(path=>path==='/admin/overview').length,2)
    assert.equal(ui.calls.filter(path=>path==='/admin/resource-usage').length,2)
    assert.equal(ui.refresh().props.disabled,true);assert.match(ui.text(),/23/)
    assert.equal(ui.nodes().filter(item=>item.props['data-chart']==='quota-gauge').length,2)
    jobs.get('/admin/overview')!.resolve(overview(51));jobs.get('/admin/resource-usage')!.resolve(resources());await flush()
    assert.match(ui.text(),/51/);assert.equal(ui.refresh().props.disabled,false)
  }finally{ui.unmount()}
})

test('读取失败保留既有统计与采购图，统一失败提示且仍只有一个重试按钮',async()=>{
  let fail=false
  const ui=mount(async path=>{if(fail)throw new api.ApiError(0,'NETWORK_ERROR','测试网络中断');return path==='/admin/overview'?overview():resources()})
  try{
    await ui.ready;const originalTimes=ui.nodes().filter(item=>item.tag==='time').map(item=>item.props.datetime)
    fail=true;ui.refresh().props.onClick();await flush()
    assert.match(ui.text(),/读取失败，已保留上次内容/);assert.match(ui.text(),/测试网络中断/)
    assert.equal(ui.nodes().filter(item=>classHas(item,'refresh-control')).length,1)
    assert.equal(ui.nodes().filter(item=>item.props['data-chart']==='quota-gauge').length,2)
    assert.deepEqual(ui.nodes().filter(item=>item.tag==='time').map(item=>item.props.datetime),originalTimes)
  }finally{ui.unmount()}
})

test('任一管理员读取明确403会清理本页所有管理员内容，不能借保旧继续显示采购',async()=>{
  for(const denied of ['/admin/overview','/admin/resource-usage']){
    let failure=false
    const ui=mount(async path=>{if(failure){if(path===denied)throw new api.ApiError(403,'permission_denied','管理员权限已失效');throw new api.ApiError(0,'NETWORK_ERROR','网络暂不可用')}return path==='/admin/overview'?overview():resources()})
    try{
      await ui.ready;failure=true;ui.refresh().props.onClick();await flush()
      assert.match(ui.text(),/管理员权限已失效/)
      assert.doesNotMatch(ui.text(),/HOME测试采购|BWH测试采购|用户总数|服务总数/)
      assert.equal(ui.nodes().filter(item=>item.props['data-chart']==='quota-gauge').length,0)
    }finally{ui.unmount()}
  }
})

test('会话撤权立即撤除旧采购和统计，迟到请求不能重新带回',async()=>{
  const jobs:Array<ReturnType<typeof deferred<unknown>>>=[]
  const ui=mount(async()=>{const job=deferred<unknown>();jobs.push(job);return job.promise})
  try{
    await ui.ready;assert.equal(jobs.length,2)
    ui.auth.session.user.is_staff=false;await flush()
    jobs[0].resolve(overview());jobs[1].resolve(resources());await flush()
    assert.doesNotMatch(ui.text(),/HOME测试采购|用户总数|服务总数/)
    assert.equal(ui.calls.length,2)
  }finally{ui.unmount()}
})

test('采购来源暂不可读保同授权旧图与原时间，来源换代不可沿用旧图',async()=>{
  let resource:any=resources()
  const ui=mount(async path=>path==='/admin/overview'?overview():resource)
  try{
    await ui.ready
    resource={items:[{service_id:'authorized-personal-fixture',provider_usage:{schema_version:2,source_key:'same-authorized-source',generated_at:null,meters:[],error:{code:'usage_unavailable',message:'测试采购来源失败'}}}]}
    ui.refresh().props.onClick();await flush()
    assert.equal(ui.nodes().filter(item=>item.props['data-chart']==='quota-gauge').length,2)
    assert.match(ui.text(),/测试采购来源失败/);assert.match(ui.text(),/统计来源暂不可更新/)
    resource.items[0].provider_usage.source_key='new-authorized-source';ui.refresh().props.onClick();await flush()
    assert.equal(ui.nodes().filter(item=>item.props['data-chart']==='quota-gauge').length,0)
  }finally{ui.unmount()}
})

test('AdminResourceUsage独立使用仍保留自身统一刷新，真实采购图未删',async()=>{
  const ui=mount(async()=>resources(),'components/AdminResourceUsage.vue')
  try{await ui.ready;assert.equal(ui.nodes().filter(item=>classHas(item,'refresh-control')).length,1);assert.equal(ui.nodes().filter(item=>item.props['data-chart']==='quota-gauge').length,2);ui.refresh().props.onClick();await flush();assert.equal(ui.calls.length,2)}finally{ui.unmount()}
})
