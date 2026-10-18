// 采购计划和个人套餐分别维护；消费回执与保存回执独立。
export type ResourcePlanOperation = 'quota' | 'expiry' | 'cycle'
export type ResourcePlanChanges = { quota_bytes: string } | { add_bytes: string }
  | { expires_on: string | null } | { days: number; next_reset_at: string }
export interface ResourcePlan {
  id: string; label: string; source: 'residential' | 'bwh'; scope: 'external_node' | 'server'
  quota_bytes: string | null; cycle: { kind: 'fixed_days'; days: number; anchor: string } | { kind: 'provider' }
  expires_on: string | null; accepted_initial_gap: boolean
}
export interface ResourcePlanRead {
  meter_id: string; revision: number; source_fingerprint: string; plan: ResourcePlan
  effective_plan: ResourcePlan
  consumption: { status: 'not_consumed' | 'pending' | 'consumed' | 'rejected'; revision: number | null; consumed_at: string | null;
    error_code?: '' | 'cycle_reset_expired'; effective_plan_fingerprint?: string | null }
  allowed_operations: ResourcePlanOperation[]
}
export interface ResourcePlanPreview extends ResourcePlanRead {
  operation: ResourcePlanOperation; changes: ResourcePlanChanges; before: ResourcePlan; after: ResourcePlan
  effects: string[]; preview_token: string
}
export interface ResourcePlanConfirm extends ResourcePlanRead {
  receipt: { id: string; revision: number; status: 'pending' | 'consumed' | 'rejected'; replayed: boolean;
    error_code?: '' | 'cycle_reset_expired'; effective_plan_fingerprint?: string | null }
}
