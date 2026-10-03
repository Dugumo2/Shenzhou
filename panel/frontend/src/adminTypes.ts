import type { Pagination, Service, Session } from './types'

export interface AdminUser {
  id: string
  username: string
  is_active: boolean
  is_staff: boolean
  service_count: number
  mapping_required: boolean
}

export interface AdminUsers {
  items: AdminUser[]
  pagination: Pagination
}

export interface AdminUserDetail {
  user: AdminUser
  services: Service[]
  capabilities: { create: boolean; quota: boolean; renew: boolean; grants: boolean; enable: boolean }
}

export interface AdminOverview {
  users: { total: number; active: number; disabled: number }
  services: {
    total: number
    expired: number
    mapping_required: number
    statistics_incomplete: number | null
    recorded_metering_gaps: number
  }
  generated_at: string
  environment: Session['environment']
  limitations: string[]
}
