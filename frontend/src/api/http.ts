import type {
  ApiErrorBody,
  EnvironmentSummary,
  MockConfig,
  SessionConfig,
  SessionSummary,
} from '@/types/contracts'

const API_ROOT = '/api/v1'

import type { LocalInterface, ModelConnectionConfig, ModelConnectionStatus, ModelDescriptor } from '@/types/modelConnection'

export class ApiError extends Error {
  readonly status: number
  readonly code?: string

  constructor(message: string, status: number, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

function detailMessage(body: ApiErrorBody | undefined): string | undefined {
  if (!body) return undefined
  if (typeof body.detail === 'string') return body.detail
  if (Array.isArray(body.detail)) {
    return body.detail.map((item) => item.msg || item.message).filter(Boolean).join('；')
  }
  const nested = (body as ApiErrorBody & { error?: { message?: string } }).error
  if (nested?.message) return nested.message
  return body.message
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (init?.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  headers.set('Accept', 'application/json')

  let response: Response
  try {
    response = await fetch(`${API_ROOT}${path}`, { ...init, headers })
  } catch (error) {
    throw new ApiError(error instanceof Error ? error.message : '无法连接 Coordinator', 0, 'NETWORK_ERROR')
  }

  if (!response.ok) {
    let body: ApiErrorBody | undefined
    try {
      body = (await response.json()) as ApiErrorBody
    } catch {
      body = undefined
    }
    throw new ApiError(detailMessage(body) || `请求失败（HTTP ${response.status}）`, response.status, body?.code)
  }

  if (response.status === 204) return undefined as T
  const contentType = response.headers.get('content-type') || ''
  if (!contentType.includes('application/json')) return (await response.text()) as T
  return (await response.json()) as T
}

function body(value: unknown): BodyInit {
  return JSON.stringify(value)
}

export const api = {
  modelConnection: () => request<ModelConnectionStatus>('/model-connection'),
  modelInterfaces: () => request<LocalInterface[]>('/model-connection/interfaces'),
  testModelConnection: (config: ModelConnectionConfig) => request<ModelDescriptor>('/model-connection/test', { method: 'POST', body: body(config) }),
  connectModel: (config: ModelConnectionConfig) => request<ModelConnectionStatus>('/model-connection', { method: 'PUT', body: body(config) }),
  bootstrap: () => request<unknown>('/bootstrap'),
  getConfig: () => request<unknown>('/config'),
  saveConfig: (config: SessionConfig) => {
    const { version, ...resource } = config
    const payload = version ? { expected_version: version, config: resource } : resource
    return request<unknown>('/config', { method: 'PUT', body: body(payload) })
  },
  listAgents: () => request<unknown>('/agents'),
  listSessions: () => request<unknown>('/sessions'),
  getSession: (id: string) => request<unknown>(`/sessions/${encodeURIComponent(id)}`),
  createSession: (config: SessionConfig) => {
    const resource = { ...config }
    delete resource.version
    return request<unknown>('/sessions', { method: 'POST', body: body({ config: resource }) })
  },
  sessionCommand: (id: string, command: 'pause' | 'resume' | 'stop') =>
    request<unknown>(`/sessions/${encodeURIComponent(id)}/${command}`, { method: 'POST' }),
  getEnvironment: () => request<unknown>('/environment'),
  saveEnvironment: (environment: EnvironmentSummary) =>
    request<unknown>('/environment', {
      method: 'PUT',
      body: body({
        version: environment.version,
        environment_id: environment.environment_id || environment.id,
        model_provider: environment.model_provider,
        network_provider: environment.network_provider,
        transfer_provider: environment.transfer_provider,
      }),
    }),
  getMock: () => request<unknown>('/mock'),
  saveMock: (config: MockConfig) =>
    request<unknown>('/mock', {
      method: 'PUT',
      body: body(
        config.links && config.model
          ? { version: config.version, links: config.links, model: config.model }
          : config,
      ),
    }),
  exportCsv: async (sessionId?: string): Promise<Blob> => {
    const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : ''
    const response = await fetch(`${API_ROOT}/experiments.csv${query}`, {
      headers: { Accept: 'text/csv' },
    })
    if (!response.ok) throw new ApiError(`CSV 导出失败（HTTP ${response.status}）`, response.status)
    return response.blob()
  },
}

export function unwrapResource<T>(value: unknown, keys: string[]): T {
  if (value && typeof value === 'object') {
    const source = value as Record<string, unknown>
    for (const key of keys) {
      if (source[key] !== undefined) return source[key] as T
    }
  }
  return value as T
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export type SessionCommand = 'pause' | 'resume' | 'stop'
export type CreateSessionResponse = SessionSummary | { session: SessionSummary }
