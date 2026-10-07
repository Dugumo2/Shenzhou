import type { ProviderUsage } from './providerUsage'
export type Bytes = string | number | null
export interface Session {
  authenticated: boolean
  user: { username: string; is_staff: boolean } | null
  csrf_token: string; brand: string; time_zone: string
  environment?: { kind: 'local_candidate' | 'production'; is_demo: boolean }
  password_policy: { min_length: number; messages: string[]; validated_by_server: boolean }
}
export interface Delivery { state: string; message: string; download_url: string | null }
export interface Compatibility { state: string; message: string | null }
export interface Service {
  provider_usage?: ProviderUsage
  id: string; name: string; quota_bytes: Bytes; used_bytes: Bytes; raw_bytes: Bytes
  remaining_bytes: Bytes; next_reset_at: string | null; expires_at: string | null
  state: string; enabled: boolean; status_label: string; source_type: 'entitlement' | 'membership' | 'p8'
  capabilities?: { billing: boolean; quota: boolean; renew: boolean; grants: boolean; enable: boolean; reset: boolean; p8_delivery: boolean }
  operation_targets?: { billing: string | null; p8_delivery: string | null }
  business_state?: string; quota_state?: 'applied' | 'configured' | 'unknown'
  usage: { quality: string; updated_at: string | null; message: string }
  delivery: Delivery
  user?: { username: string }
  actions?: { billing: boolean; quota: boolean; renew: boolean; grants: boolean; enable: boolean; reset: boolean }
  clients?: { id: string; delivery: Delivery }[]
  compatibility?: Compatibility
}
export interface Client {
  id: string; name: string; device: 'computer' | 'phone' | 'router'; os: string | null
  verification: 'not_tested' | 'unsupported' | 'verified'
  software_version: string | null; core_version: string | null
  resources: { kind: string; label: string }[]; guide_url: string; reason: string
}
export interface Guide {
  client_id: string; title: string; verification: string
  software_version: string | null; core_version: string | null
  steps: { title: string; body: string }[]
  update_status: { published: string; downloaded: string; applied: string }
  limitations: string[]
}
export interface Pagination { page: number; page_size: number; total: number; pages: number; has_next: boolean; has_previous: boolean }
export interface Billing {
  service_id: string; billing_revision: number
  current_cycle: { id: number | string; starts_at: string; ends_at: string; used_bytes: Bytes; raw_bytes: Bytes; weighted_remainder: string | number | null } | null
  plan: { next_reset_at: string; anchor_day: number; hour: number; minute: number } | null
  quota_bytes: Bytes; used_bytes: Bytes; remaining_bytes: Bytes
  usage_quality: string; expires_at: string | null; can_modify: boolean; blocked_reason: string | null
}
export interface BillingPreview extends Billing {
  old_next_reset_at: string; next_reset_at: string; shift_seconds: number
  next_resets: string[]; expires_before_reset: boolean; preview_token: string; preview_expires_at: string
}
