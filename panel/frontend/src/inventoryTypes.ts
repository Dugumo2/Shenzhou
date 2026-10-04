import type { Pagination } from './types'

export interface RegisteredServer { id: string; name: string; enabled: boolean }
export type Freshness = 'fresh' | 'stale' | 'target_changed' | 'unknown'
export interface ResourceMetadata { id: string; name: string; notes: string; revision: number }
export interface Observation {
  source: string; observed_at: string; expires_at: string; freshness: Freshness; scope: string
  metrics: { online: boolean | null; cpu_percent: number | null; cpu_window_seconds: number | null;
    memory_percent: number | null; disk_percent: number | null; network_rx_bytes: number | null;
    network_tx_bytes: number | null; network_interfaces: string[] | null }
}
export interface InventoryCheck {
  id: string; target_kind: string; target_id: string; source: string; check_kind: string;
  result: 'pass' | 'fail' | 'timeout' | 'unknown'; latency_ms: number | null; error_stage: string; error_code: string;
  observed_at: string; expires_at: string; freshness: Freshness
}
export interface CoreMetadata {
  id: string; name: string; instance_key: string; core_type: string; registered_version: string | null;
  configuration_owner: string; configuration_version: string; revision: number; notes: string;
  ingress_ids: string[]; association_valid: boolean
}
export interface ServerInventory extends RegisteredServer {
  notes: string; revision: number
  adapter: string; last_seen_at: string | null; ingress_count: number; egress_count: number
  monitoring: { state: Freshness | 'not_connected'; online: boolean | null; observed_at: string | null; expires_at?: string; source?: string }
}
export interface RegisteredIngress {
  id: string; name: string; protocol: string; protocol_label: string; enabled: boolean
  server: RegisteredServer
}
export interface RegisteredEgress {
  id: string; name: string; kind: string; kind_label: string; fail_closed: boolean
  server: RegisteredServer
}
export interface InventoryList<T> { items: T[]; pagination: Pagination; read_only: boolean; limitations: string[] }
export interface ServerInventoryDetail {
  server: ServerInventory; read_only: boolean
  metrics: { cpu_percent: null; memory_percent: null; disk_percent: null; upload_bps: null; download_bps: null; total_transfer_bytes: null }
  core: { actual_version: null; state: 'not_connected' }
  observation: Observation | null; cores: { items: CoreMetadata[]; truncated: boolean }
  checks: { items: InventoryCheck[]; truncated: boolean }
  ingresses: { items: RegisteredIngress[]; total: number; truncated: boolean }
  egresses: { items: RegisteredEgress[]; total: number; truncated: boolean }
  capabilities: { edit: boolean; probe: false; manage_cores: false }; limitations: string[]
}
export interface LineInventory {
  notes: string; revision: number
  id: string; name: string; enabled: boolean; ingresses: RegisteredIngress[]; ingress_count: number
  ingresses_truncated: boolean; egress: RegisteredEgress; endpoint_registration: 'enabled' | 'disabled'
  verification: { state: Freshness | 'not_tested'; observed_at: string | null; result?: InventoryCheck['result']; expires_at?: string; source?: string }
}
export interface LineInventoryDetail {
  line: LineInventory; read_only: boolean
  checks: { items: InventoryCheck[]; truncated: boolean }
  capabilities: { edit: boolean; probe: false; publish: false }; limitations: string[]
}
