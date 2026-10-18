import type { Pagination } from './types'
import type { ResourceUsageMeter } from './providerUsage'

export interface RegisteredServer { id: string; name: string; enabled: boolean }
export type Freshness = 'fresh' | 'stale' | 'target_changed' | 'unknown'
export type PurchaseMeterState = 'unbound' | 'ready' | 'unavailable' | 'forbidden'
export interface ResourceMetadata { id: string; name: string; notes?: string; revision: number }
export interface Observation {
  source: string; observed_at: string; expires_at: string; freshness: Freshness; scope: string
  rate_state?: 'unavailable' | 'valid' | 'stale' | 'generation_changed' | 'counter_reset' | 'gap' | 'scope_changed'
  metrics: { online: boolean | null; cpu_percent: number | null; cpu_window_seconds: number | null;
    memory_percent: number | null; disk_percent: number | null; network_rx_bytes: number | null;
    network_tx_bytes: number | null; network_interfaces: string[] | null;
    network_rx_bytes_per_second?: number | null; network_tx_bytes_per_second?: number | null;
    network_window_seconds?: number | null }
}
export interface InventoryCheck {
  id: string; target_kind: string; target_id: string; source: string; check_kind: string;
  result: 'pass' | 'fail' | 'timeout' | 'unknown'; latency_ms: number | null; error_stage: string; error_code: string;
  observed_at: string; expires_at: string; freshness: Freshness
  target_version?: string
}
export interface CoreMetadata {
  id: string; name: string; instance_key: string; core_type: string; registered_version: string | null;
  configuration_owner: string; configuration_version: string; revision: number; notes: string;
  ingress_ids: string[]; association_valid: boolean
  service_unit?: string | null; owner_state?: CoreOwnerState
  evidence_source?: string; evidence_observed_at?: string | null
}
export interface ServerInventory extends RegisteredServer {
  notes: string; revision: number
  adapter: string; last_seen_at: string | null; ingress_count: number; egress_count: number
  observation?: Observation | null; core_count?: number; node_count?: number; purchase_meter_id?: string | null
  purchase_meter_state?: PurchaseMeterState; purchase_binding_key?: string | null
  monitoring: { state: Freshness | 'not_connected'; online: boolean | null; observed_at: string | null; expires_at?: string; source?: string }
}
export interface RegisteredIngress {
  id: string; name: string; protocol: string; protocol_label: string; enabled: boolean
  server: RegisteredServer
  revision?: number; endpoint_host?: string | null; endpoint_port?: number | null
  evidence_source?: string; evidence_observed_at?: string | null
}
export interface RegisteredEgress {
  id: string; name: string; kind: string; kind_label: string; fail_closed: boolean
  server: RegisteredServer
  revision?: number; ownership?: 'unknown' | 'self_owned' | 'external'; external_kind?: 'node' | 'subscription' | null
  protocol?: string | null; endpoint_host?: string | null; endpoint_port?: number | null
  evidence_source?: string; evidence_observed_at?: string | null; purchase_meter_id?: string | null
  core_instance_id?: string | null; next_egress_id?: string | null
}
export interface InventoryList<T> { items: T[]; pagination: Pagination; read_only: boolean; limitations: string[] }
export interface ServerInventoryDetail {
  server: ServerInventory; read_only: boolean
  metrics: { cpu_percent: number | null; memory_percent: number | null; disk_percent: number | null; upload_bps: number | null; download_bps: number | null; total_transfer_bytes: number | null }
  core: { actual_version: null; state: 'not_connected' }
  observation: Observation | null; cores: { items: CoreMetadata[]; truncated: boolean }
  checks: { items: InventoryCheck[]; truncated: boolean }
  ingresses: { items: RegisteredIngress[]; total: number; truncated: boolean }
  egresses: { items: RegisteredEgress[]; total: number; truncated: boolean }
  purchase_meter?: ResourceUsageMeter | null; nodes?: NodeResourcePage; check_actions?: InventoryCheckAction[]
  purchase_meter_state?: PurchaseMeterState; purchase_binding_key?: string | null
  purchase_plan_editable?: boolean
  capabilities: { edit: boolean; probe: boolean; manage_cores: false }; limitations: string[]
}
export interface LineInventory {
  notes: string; revision: number
  id: string; name: string; enabled: boolean; ingresses: RegisteredIngress[]; ingress_count: number
  ingresses_truncated: boolean; egress: RegisteredEgress; endpoint_registration: 'enabled' | 'disabled'
  verification: { state: Freshness | 'not_tested'; observed_at: string | null; result?: InventoryCheck['result']; expires_at?: string; source?: string }
  target_version?: string | null; path?: LinePathProjection
}
export interface LineInventoryDetail {
  line: LineInventory; read_only: boolean
  checks: { items: InventoryCheck[]; truncated: boolean }
  nodes?: NodeResourcePage; check_actions?: InventoryCheckAction[]
  capabilities: { edit: boolean; probe: boolean; publish: false }; limitations: string[]
}

export type CoreOwnerState = 'unknown' | 'observed_only' | 'verified'
export interface NodeResourcePage { items: NodeResource[]; total: number; truncated: boolean }
export interface NodeResource {
  id: string; resource_id: string; resource_type: 'ingress' | 'egress'; name: string
  kind: 'owned' | 'external' | 'unknown'; protocol: string | null; protocol_label: string
  enabled: boolean | null; revision: number; endpoint: { host: string | null; port: number | null }
  execution_server: RegisteredServer
  core_instance_id?: string | null; next_egress_id?: string | null
  next_resource?: { id: string; resource_id: string; name: string } | null
  core_refs: { state: 'unknown' | 'conflict' | 'linked'; items: { id: string; name: string; core_type: string; owner_state: CoreOwnerState }[] }
  external_kind: 'node' | 'subscription' | null; evidence: { source: string | null; observed_at: string | null }
  purchase_meter: ResourceUsageMeter | null; purchase_meter_id: string | null
  purchase_binding_key?: string | null; purchase_plan_editable?: boolean
  capabilities: { manage_core: false; install_agent: false; grant: false; sync_source: false }; limitations: string[]
}
export interface LinePathPoint { id: string; name: string; kind: 'ingress' | 'egress' | 'core' | 'unknown' }
export interface LineSegmentCapability {
  state: 'unknown' | 'supported' | 'unsupported' | 'pass' | 'fail' | 'timeout'
  source: string; observed_at: string | null; expires_at: string | null; freshness: Freshness
}
export interface LinePathSegment {
  id: string; from: LinePathPoint; to: LinePathPoint; relation: 'registered' | 'observed' | 'unknown'
  protocol: string | null; core: { id: string; name: string; core_type: string } | null
  tcp: LineSegmentCapability; udp: LineSegmentCapability; dns: LineSegmentCapability
  authentication: 'registered' | 'unknown'; failure_behavior: 'fail_closed_required' | 'unknown'
  evidence_source?: string; evidence_observed_at?: string | null
}
export interface LinePathProjection {
  kind: 'registered_association' | 'observed_path' | 'single_node'; source: string; observed_at: string | null
  segments: LinePathSegment[]; warnings: string[]
  terminal_egress?: RegisteredEgress | null
}
export interface InventoryCheckAction {
  action: string; target_kind: string; target_id: string; target_version: string; label: string
  timeout_seconds: number; min_interval_seconds: number; scope: string
}
export interface InventoryCheckJob {
  id: string; target_kind: string; target_id: string; target_version: string; action: string
  state: 'queued' | 'running' | 'completed'; created_at: string; started_at: string | null
  finished_at: string | null; lease_until: string | null; failure_code: string; result: InventoryCheck | null
}
