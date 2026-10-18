// 核心控制API只投影有限差异和回执，不承载运行配置或凭据。
export type CoreAction = 'check' | 'apply' | 'rollback'
export type CoreLogLevel = 'debug' | 'info' | 'warning' | 'error' | 'none'
export interface CoreCandidate {
  id: string; core_id: string; base_revision: number
  state: 'prepared' | 'checked' | 'check_failed' | 'applied' | 'rolled_back'
  patch: { log: { level: CoreLogLevel | null } }
  difference: { field: string; before: CoreLogLevel | null; after: CoreLogLevel | null }
  base_sha256: string | null; candidate_sha256: string | null
  created_at: string; checked_at: string | null
  capabilities: Record<CoreAction, boolean>
  impact: { scope: 'single_core'; may_restart: boolean; message: string }
}
export interface CoreControlJob {
  id: string; core_id: string; candidate_id: string; action: CoreAction
  status: 'queued' | 'running' | 'unknown' | 'succeeded' | 'failed'
  expected_revision: number; fence: number; error_code: string
  result: { status?: string; error_code?: string; job_id?: string; core_id?: string;
    action?: CoreAction; fence?: number; config_sha256?: string; candidate_sha256?: string;
    binding_fingerprint?: string; core_version?: string }
  created_at: string; started_at: string | null; finished_at: string | null
}
export interface CoreControlDetail {
  core_id: string; revision: number; configuration_version: string; owner_state: string
  binding: { available: boolean; reason: string }; busy: boolean; write_enabled: boolean
  current_applied_candidate_id: string | null
  capabilities: { create_candidate: boolean; supported_fields: string[]; log_levels: CoreLogLevel[] }
  candidates: CoreCandidate[]; jobs: CoreControlJob[]; limitations: string[]
}
