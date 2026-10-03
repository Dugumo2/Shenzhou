import type { Pagination } from './types'

export interface RegisteredServer { id: string; name: string; enabled: boolean }
export interface ServerInventory extends RegisteredServer {
  adapter: string; last_seen_at: string | null; ingress_count: number; egress_count: number
  monitoring: { state: 'not_connected'; online: null; observed_at: null }
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
  ingresses: { items: RegisteredIngress[]; total: number; truncated: boolean }
  egresses: { items: RegisteredEgress[]; total: number; truncated: boolean }
  capabilities: { edit: false; probe: false; manage_cores: false }; limitations: string[]
}
export interface LineInventory {
  id: string; name: string; enabled: boolean; ingresses: RegisteredIngress[]; ingress_count: number
  ingresses_truncated: boolean; egress: RegisteredEgress; endpoint_registration: 'enabled' | 'disabled'
  verification: { state: 'not_tested'; observed_at: null }
}
export interface LineInventoryDetail {
  line: LineInventory; read_only: boolean
  capabilities: { edit: false; probe: false; publish: false }; limitations: string[]
}
