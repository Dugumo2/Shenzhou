import { shallowReactive } from 'vue'
import { ApiError, request } from './api.ts'

export interface PolicyRule { action:'proxy'|'client_direct'; kind:'exact'|'suffix'|'regex'; value:string; scope_domain:string; enabled:boolean }
export type Component = {type:'source';source_id:string;revision:number;enabled:boolean}|{type:'custom';rule_id:number;revision:number;enabled:boolean}
export interface PolicyItem { policy_id:string;name:string;revision:number;updated_at:string }
export interface PolicyEntry {key:string;component:number;source_name:string;origin:'source'|'custom';rule:PolicyRule;enabled:boolean;overridden_by?:string|null}
export interface PolicyDocument {components:Component[];overrides:Record<string,string|null>;entries:PolicyEntry[]}
export interface Binding {binding_id:string;service_id:string;service_source:string;revision:number;fence:number;publisher:string;current_candidate_id:string|null}
export interface Candidate {candidate_id:string;fence:number;manifest:Record<string,unknown>;artifact:{schema_version:number;rules:Record<string,unknown>[]};sha256:string;production_published:false}
export interface PolicyDetail {item:PolicyItem;version:{revision:number;sha256:string;document:PolicyDocument};bindings:Binding[];history:{revision:number;sha256:string}[];read_only:boolean}
export interface PolicyOptions {sources:{source_id:string;name:string;revision:number;count:number;keys:{key:string;rule:PolicyRule}[]}[];custom:{rule_id:number;revision:number;key:string;rule:PolicyRule}[];sources_truncated:boolean;custom_truncated:boolean;read_only:boolean;limitations:string[]}
export interface MatchExplanation {domain:string;matches:{key:string;order:number;source_name:string;action:string;enabled:boolean;overridden:boolean}[];final_action:string;reason:string;revision:number;production_published:false}
export interface PolicyDraft {name:string;components:Component[];overrides:Record<string,string|null>}
export interface PolicyPending {path:string;method:string;body:Record<string,unknown>}
// 切换页面不丢失不确定请求；按已核验管理员隔离，重试始终使用原幂等键。
const pendingPolicies=shallowReactive(new Map<string,PolicyPending>())
export function getPolicyPending(identity:string):PolicyPending|null{return pendingPolicies.get(identity)||null}
export function rememberPolicyPending(identity:string,pending:PolicyPending):PolicyPending{
  const previous=pendingPolicies.get(identity);if(previous)return previous
  const frozen=JSON.parse(JSON.stringify(pending)) as PolicyPending;pendingPolicies.set(identity,frozen);return frozen
}
export function clearPolicyPending(identity:string){pendingPolicies.delete(identity)}

export function moveComponent(components:Component[], index:number, offset:number):Component[] {
  const result=JSON.parse(JSON.stringify(components)) as Component[], target=index+offset
  if(index<0||index>=result.length||target<0||target>=result.length)return result
  const [row]=result.splice(index,1);result.splice(target,0,row!);return result
}
export function capturePolicySave(draft:PolicyDraft, item:PolicyItem|null, key:string):PolicyPending {
  return {path:'/admin/rule-policies'+(item?'/'+encodeURIComponent(item.policy_id):''),method:item?'PATCH':'POST',
    body:JSON.parse(JSON.stringify({...draft,expected_revision:item?.revision||0,idempotency_key:key}))}
}
export function uncertainFailure(error:unknown):boolean {
  return !(error instanceof ApiError)||error.status===0||error.status>=500||error.code==='INVALID_RESPONSE'
}
export async function submitPolicyPending<T>(pending:PolicyPending):Promise<T> {
  return request<T>(pending.path,pending.method,pending.body)
}
