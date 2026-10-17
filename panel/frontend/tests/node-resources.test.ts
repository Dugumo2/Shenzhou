import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import { parse, compileScript } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import ts from 'typescript'
import * as vue from 'vue'
import type { NodeResource } from '../src/inventoryTypes.ts'
import type { ResourceUsageMeter } from '../src/providerUsage.ts'

const require = createRequire(import.meta.url)
const root = fileURLToPath(new URL('../src/', import.meta.url))
function node(changes: Partial<NodeResource> = {}): NodeResource {
  return { id:'ingress:1',resource_id:'1',resource_type:'ingress',name:'真实接入端点',kind:'owned',protocol:'reality',
    protocol_label:'Reality',enabled:true,revision:1,endpoint:{host:'node.example.com',port:443},
    execution_server:{id:'server-1',name:'BWH',enabled:true},
    core_refs:{state:'linked',items:[{id:'core-1',name:'已有 Xray',core_type:'xray',owner_state:'observed_only'}]},
    external_kind:null,evidence:{source:'已审盘点',observed_at:'2026-10-17T10:00:00+08:00'},
    purchase_meter:null,purchase_meter_id:null,capabilities:{manage_core:false,install_agent:false,grant:false,sync_source:false},
    limitations:['资源资料与用户授权、倍率及套餐账本分别维护。'],...changes }
}
function meter(): ResourceUsageMeter {
  return {id:'home',label:'HOME',scope:'external_node',source_kind:'estimate',quality:'current',quota_bytes:'400000000000',
    used_bytes:'100000000000',remaining_bytes:'300000000000',upload_bytes:'20000000000',download_bytes:'80000000000',
    observed_at:'2026-10-17T10:00:00+08:00',expires_at:'2099-10-17T10:03:00+08:00',expires_on:'2027-01-19',
    cycle:{kind:'fixed_days',starts_at:'2026-09-20T00:00:00+08:00',ends_at:'2026-10-20T00:00:00+08:00',next_reset_at:'2026-10-20T00:00:00+08:00'},
    alerts:[],details:{}}
}
function external(changes: Partial<NodeResource> = {}): NodeResource {
  return node({id:'egress:1',resource_type:'egress',name:'HOME',kind:'external',external_kind:'node',protocol:'ss2022',
    protocol_label:'Shadowsocks 2022',enabled:null,core_refs:{state:'unknown',items:[]},purchase_meter_id:'home',purchase_meter:meter(),...changes})
}

// 执行真实列表与详情的 SFC 模板；不复制业务渲染逻辑。
async function render(nodes: NodeResource[]) {
  const cache = new Map<string, any>()
  function load(filename: string): any {
    if (cache.has(filename)) return cache.get(filename)
    const source = readFileSync(filename, 'utf8')
    const content = filename.endsWith('.vue') ? compileScript(parse(source).descriptor, { id:filename, inlineTemplate:true }).content : source
    const code = ts.transpileModule(content, {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
    const exports: Record<string, any> = {}
    cache.set(filename,exports)
    new Function('require','exports',code)((name: string) => name === 'vue' ? vue : !name.startsWith('.') ? require(name)
      : load(resolve(dirname(filename), /\.(vue|ts)$/.test(name) ? name : name+'.ts')), exports)
    return exports
  }
  return renderToString(vue.createSSRApp(load(resolve(root,'components/NodeResourceList.vue')).default,{nodes}))
}

test('真实节点列表可展开详情且保留四协议与实际执行核心',async()=>{
  const html = await render(['Reality','Hysteria 2','AnyTLS','WebSocket'].map((label,index)=>node({id:`ingress:${index}`,protocol_label:label})))
  for (const label of ['Reality','Hysteria 2','AnyTLS','WebSocket','已有 Xray','node.example.com','443']) assert.ok(html.includes(label),label)
  assert.equal((html.match(/<details/g)||[]).length,4)
  assert.equal((html.match(/资源详情/g)||[]).length,4)
})

test('HOME外部详情保留采购圆环构成图及固定30天期限，不提供自有机控制',async()=>{
  const html = await render([external()])
  for (const label of ['外部节点','执行服务器','Shadowsocks 2022','400 GB','每 30 天重置','2027-01-19','quota-gauge','transfer-composition']) assert.ok(html.includes(label),label)
  assert.doesNotMatch(html,/<button|安装Agent|安装 Agent|升级核心|管理核心|用户剩余/)
  assert.ok(html.includes('续费或替换资源不会重置用户套餐'))
})

test('外部上游来源与用户输出订阅区分，缺采购不伪造额度',async()=>{
  const html = await render([external({external_kind:'subscription',purchase_meter_id:null,purchase_meter:null})])
  assert.match(html,/上游订阅来源/)
  assert.match(html,/新增来源节点须独立授权/)
  assert.match(html,/用户输出订阅保留在服务详情/)
  assert.match(html,/尚未关联采购统计/)
  assert.doesNotMatch(html,/400 GB|quota-gauge|<button/)
})

test('未知和冲突核心不冒称关联已核实；额外认证字段不进入模板',async()=>{
  const unknown = node({core_refs:{state:'unknown',items:[]}})
  const conflict = node({id:'ingress:2',core_refs:{state:'conflict',items:[]}})
  Object.assign(unknown,{password:'PRIVATE-PASSWORD',credential_ref:'PRIVATE-UUID',config:'PRIVATE-CONFIG',uri:'vless://PRIVATE'})
  const html = await render([unknown,conflict,external({kind:'unknown',purchase_meter:null})])
  assert.match(html,/核心归属待核实/);assert.match(html,/核心关联冲突/);assert.match(html,/所有权待核实/)
  assert.doesNotMatch(html,/PRIVATE|已登记核心关联|quota-gauge/)
})

test('更名不变采购金额周期，缺数据保留未知，过期仍显示旧图形',async()=>{
  const old = meter();old.quality='stale';old.expires_at='2026-09-01T00:00:00Z';old.remaining_bytes=null
  const html = await render([external({name:'更名的外部资源',purchase_meter:old})])
  assert.match(html,/更名的外部资源/);assert.match(html,/更新延迟/);assert.match(html,/暂不可确认/)
  assert.match(html,/400 GB/);assert.match(html,/100 GB/);assert.match(html,/每 30 天重置/)
  assert.match(html,/quota-gauge/);assert.match(html,/transfer-composition/)
})

test('真实双核心详情区分原SOCKS中转与外部SS2022并显示后续引用',async()=>{
  const socks = node({id:'egress:1',resource_type:'egress',name:'HOME 原中转',protocol:'socks',protocol_label:'SOCKS 中转',
    endpoint:{host:'127.0.0.1',port:18080},next_resource:{id:'egress:3',resource_id:'3',name:'HOME 外部 SS2022'},
    core_refs:{state:'linked',items:[{id:'core-sb',name:'原 sing-box',core_type:'sing_box',owner_state:'observed_only'}]}})
  const tail = external({id:'egress:3',name:'HOME 外部 SS2022',endpoint:{host:null,port:54097},
    core_refs:{state:'linked',items:[{id:'core-xray',name:'Xray 出站核心',core_type:'xray',owner_state:'observed_only'}]}})
  const html = await render([socks,tail])
  for(const label of ['SOCKS 中转','127.0.0.1','18080','后续资源','原 sing-box','Xray 出站核心','Shadowsocks 2022','54097']) assert.ok(html.includes(label),label)
  assert.equal((html.match(/data-chart="quota-gauge"/g)||[]).length,1)
  assert.match(html,/不代表拥有供应商的服务器或核心/)
  assert.doesNotMatch(html,/<button|安装Agent|升级核心/)
})
