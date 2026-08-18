import type {
  CreateJobResponse,
  HealthResponse,
  ImportJobRequest,
  JobSnapshot,
  LocalAccountList,
  Recover401JobRequest,
  Sub2API401Scan,
  Sub2APIConfig,
  Sub2APIOptions,
  Sub2APIConfigUpdate,
} from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })

  let body: unknown
  try {
    body = await response.json()
  } catch {
    throw new Error(`服务返回异常（HTTP ${response.status}）`)
  }

  if (!response.ok) {
    const payload = body as { detail?: string | Array<{ msg?: string; loc?: Array<string | number> }> }
    if (Array.isArray(payload.detail)) {
      const message = payload.detail.map((item) => item.msg || '参数错误').join('；')
      throw new Error(message)
    }
    throw new Error(payload.detail || `请求失败（HTTP ${response.status}）`)
  }
  return body as T
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/api/health')
}

export function getSub2APIConfig(): Promise<Sub2APIConfig> {
  return request<Sub2APIConfig>('/api/config/sub2api')
}

export function saveSub2APIConfig(payload: Sub2APIConfigUpdate): Promise<Sub2APIConfig> {
  return request<Sub2APIConfig>('/api/config/sub2api', {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function getSub2APIOptions(): Promise<Sub2APIOptions> {
  return request<Sub2APIOptions>('/api/config/sub2api/options')
}

export function createImportJob(payload: ImportJobRequest): Promise<CreateJobResponse> {
  return request<CreateJobResponse>('/api/jobs', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function scanSub2API401Accounts(): Promise<Sub2API401Scan> {
  return request<Sub2API401Scan>('/api/sub2api/accounts/401')
}

export function createRecover401Job(payload: Recover401JobRequest): Promise<CreateJobResponse> {
  return request<CreateJobResponse>('/api/jobs/recover-401', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getLocalAccountRecords(limit = 20): Promise<LocalAccountList> {
  return request<LocalAccountList>(`/api/records/accounts?limit=${limit}`)
}

export function getJob(jobId: string): Promise<JobSnapshot> {
  return request<JobSnapshot>(`/api/jobs/${encodeURIComponent(jobId)}`)
}
