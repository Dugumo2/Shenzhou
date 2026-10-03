export interface DeliveryResource {
  key: string
  label: string
  download_url: string
  filename: string
  content_type: string
  bytes: number
  sha256: string
}

export interface CandidateDelivery {
  service_id: string
  service_revision: number
  client_id: string
  mode: 'synthetic'
  runtime_acceptance: 'NOT TESTED'
  state: 'ready' | 'not_prepared' | 'blocked'
  message: string
  download_url: string | null
  resources: DeliveryResource[]
  policy_sha256: string | null
  updates_available: boolean
  update_error: string
  rule_source: 'current_candidate_custom_rules'
  unsupported_resources: string[]
}
