import test from 'node:test'
import assert from 'node:assert/strict'
import { reactive } from 'vue'
import { ApiError } from '../src/api.ts'
import { capturePolicySave, moveComponent, rememberPolicyPending, getPolicyPending, clearPolicyPending, uncertainFailure, submitPolicyPending } from '../src/rulePolicies.ts'
import type { PolicyDraft } from '../src/rulePolicies.ts'

function draft():PolicyDraft{return {name:'',components:[{type:'custom',rule_id:1,revision:2,enabled:true},{type:'source',source_id:'source',revision:3,enabled:false}],overrides:{'source/a/1/0':'custom/1/2'}}}

test('响应式编辑快照可提交且后续编辑不改原请求',()=>{
  const edit=reactive(draft()),saved=capturePolicySave(edit,null,'fixed-key')
  edit.components.reverse();edit.overrides['source/a/1/0']=null
  assert.equal(saved.body.expected_revision,0)
  assert.equal((saved.body.components as PolicyDraft['components'])[0]!.type,'custom')
  assert.equal((saved.body.overrides as PolicyDraft['overrides'])['source/a/1/0'],'custom/1/2')
})
test('顺序移动保留版本与启停且不改旧快照',()=>{
  const edit=reactive(draft().components),moved=moveComponent(edit,1,-1)
  assert.equal(moved[0]!.type,'source');assert.equal(moved[0]!.revision,3);assert.equal(moved[0]!.enabled,false)
  assert.equal(edit[0]!.type,'custom');assert.deepEqual(moveComponent(edit,0,-1),edit)
})
test('页面重新进入仍使用原幂等请求并按管理员隔离',()=>{
  clearPolicyPending('a');clearPolicyPending('b')
  const original=rememberPolicyPending('a',capturePolicySave(draft(),null,'key-a'))
  const next=rememberPolicyPending('a',capturePolicySave(draft(),null,'key-other'))
  assert.equal(next,original);assert.equal(getPolicyPending('a')!.body.idempotency_key,'key-a');assert.equal(getPolicyPending('b'),null)
  clearPolicyPending('a');assert.equal(getPolicyPending('a'),null)
})
test('网络或坏响应结果不明时保持请求，字段及版本拒绝可编辑',()=>{
  assert.equal(uncertainFailure(new ApiError(0,'NETWORK_ERROR','断线')),true)
  assert.equal(uncertainFailure(new ApiError(200,'INVALID_RESPONSE','正文错误')),true)
  assert.equal(uncertainFailure(new ApiError(409,'revision_conflict','版本变化')),false)
  assert.equal(uncertainFailure(new ApiError(422,'invalid_fields','输入错误')),false)
})
test('重试发送相同版本与幂等键而不是重新组装最新草稿',async()=>{
  const old=globalThis.fetch,bodies:string[]=[]
  globalThis.fetch=async(_url,init)=>{bodies.push(String(init?.body));return new Response(JSON.stringify({data:{ok:true},error:null}),{status:200})}
  try{const pending=capturePolicySave(reactive(draft()),{policy_id:'policy',name:'默认规则方案',revision:4,updated_at:''},'same-key');await submitPolicyPending(pending);await submitPolicyPending(pending);assert.equal(bodies[0],bodies[1]);assert.equal(JSON.parse(bodies[0]!).expected_revision,4)}finally{globalThis.fetch=old}
})
