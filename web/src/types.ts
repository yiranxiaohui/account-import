export type JobStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed'
export type EventLevel = 'info' | 'success' | 'warning' | 'error'

export interface JobEvent {
  time: string
  level: EventLevel
  message: string
}

export interface ImportSummary {
  operation?: 'recover_401'
  health?: Record<string, unknown>
  redeem?: Record<string, unknown>
  download?: Record<string, unknown>
  import?: Record<string, unknown>
  scan?: Record<string, unknown>
  recovery?: Record<string, unknown>
}

export interface JobSnapshot {
  id: string
  status: JobStatus
  stage: string
  progress: number
  message: string
  created_at: string
  updated_at: string
  summary: ImportSummary
  error?: string | null
  events: JobEvent[]
}

export interface ImportJobRequest {
  redeem_base_url: string
  card_codes: string[]
  mode: 'all' | '401'
  proxy_id?: number | null
}

export interface Sub2APIConfigUpdate {
  base_url: string
  access_token?: string
  verify_tls: boolean
  group_id?: number | null
}

export interface Sub2APIConfig {
  configured: boolean
  base_url?: string | null
  has_token: boolean
  verify_tls: boolean
  group_id?: number | null
  updated_at?: string | null
}

export interface Sub2APIGroupOption {
  id: number
  name: string
  platform: string
}

export interface Sub2APIProxyOption {
  id: number
  name: string
  protocol: string
  host: string
  port: number
  account_count: number
}

export interface Sub2APIOptions {
  groups: Sub2APIGroupOption[]
  proxies: Sub2APIProxyOption[]
}

export interface Sub2API401Account {
  id: number
  name: string
  email?: string | null
  platform: string
  type: string
  status: string
  error_message: string
  card_code?: string | null
}

export interface Sub2API401Scan {
  scanned: number
  detected_401: number
  recoverable: number
  missing_card_code: number
  unique_codes: number
  accounts: Sub2API401Account[]
}

export interface Recover401JobRequest {
  redeem_base_url: string
  account_ids: number[]
}

export interface LocalAccountRecord {
  id: number
  sub2api_base_url: string
  sub2api_account_id: number
  email: string
  card_code: string
  platform: string
  last_operation: 'import' | 'recover_401'
  last_status: 'success' | 'failed'
  last_job_id: string
  last_message: string
  first_recorded_at: string
  updated_at: string
}

export interface LocalAccountList {
  total: number
  items: LocalAccountRecord[]
}

export interface CreateJobResponse {
  job: JobSnapshot
}

export interface HealthResponse {
  status: 'ok'
  sdk_version: string
}
