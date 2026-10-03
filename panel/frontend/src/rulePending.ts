import { shallowReactive } from 'vue'
import { ApiError } from './api.ts'
import { boundedRequest } from './billingPending.ts'

export interface RuleSnapshot {
  id:number; action:string; kind:string; value:string|null; scope_domain:string|null
  enabled:boolean; revision:number; validation:string
}
export interface RuleDraft { action:string; kind:string; value:string; scope_domain:string; enabled:boolean }
export interface RuleOperation {
  path:string; method:'POST'|'PATCH'|'DELETE'; body:Record<string,unknown>
  rule:RuleSnapshot|null; draft:RuleDraft
}
// 按已核验管理员隔离，只保留本页内存；页面组件卸载不会丢失幂等键。
const pending = shallowReactive(new Map<string, RuleOperation>())
const running = new Map<string, Promise<unknown>>()
export function rulePendingKey(username:string):string { return JSON.stringify([username, 'local_rules']) }
export function getRulePending(key:string):RuleOperation|null { return pending.get(key) || null }
export function rememberRulePending(key:string, operation:RuleOperation):RuleOperation {
  const previous = pending.get(key)
  if (previous) return previous
  const snapshot = Object.freeze({...operation, body:Object.freeze({...operation.body}),
    draft:Object.freeze({...operation.draft}), rule:operation.rule ? Object.freeze({...operation.rule}) : null})
  pending.set(key, snapshot)
  return snapshot
}
export function ruleRejection(error:unknown):boolean {
  if (!(error instanceof ApiError)) return false
  return (error.status === 409 && ['revision_conflict','duplicate_rule','idempotency_conflict'].includes(error.code)) ||
    (error.status === 422 && ['invalid_fields','invalid_rule','invalid_json'].includes(error.code)) ||
    (error.status === 404 && error.code === 'not_found') ||
    (error.status === 415 && error.code === 'unsupported_media_type')
}
export function submitRulePending<T>(key:string, operation:RuleOperation, send:()=>Promise<T>, timeoutMs=15_000):Promise<T> {
  const attemptKey = JSON.stringify([key, operation.method, operation.path, operation.body.idempotency_key])
  const active = running.get(attemptKey)
  if (active) return active as Promise<T>
  const clearMatching = () => {
    if (pending.get(key)?.body.idempotency_key === operation.body.idempotency_key) pending.delete(key)
  }
  const attempt = Promise.resolve().then(()=>boundedRequest(send(), timeoutMs)).then(result=>{
    clearMatching(); return result
  }, error=>{
    if (ruleRejection(error)) clearMatching()
    throw error
  }).finally(()=>{ if(running.get(attemptKey) === attempt) running.delete(attemptKey) })
  running.set(attemptKey, attempt)
  return attempt
}
