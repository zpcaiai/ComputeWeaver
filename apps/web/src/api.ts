import { currentAccessToken, type RuntimeConfig } from './oidc'

export class ApiError extends Error {
  constructor(public status: number, message: string, public correlationId?: string) { super(message) }
}

export type RequestControls = {
  idempotencyKey?: string
  ifMatch?: string
  correlationId?: string
}

export type ApiResponse<T = unknown> = {
  data: T
  status: number
  etag: string | null
  correlationId: string | null
}

export type ApiTransport = Pick<ApiClient, 'request' | 'requestDetailed'>

export class ApiClient {
  constructor(private config: RuntimeConfig) {}

  isAuthenticated(): boolean {
    return Boolean(currentAccessToken() || this.config.dev_identity)
  }

  async requestDetailed(path: string, init: RequestInit = {}, controls: RequestControls = {}): Promise<ApiResponse> {
    const headers = new Headers(init.headers)
    headers.set('Accept', 'application/json')
    const token = currentAccessToken()
    if (token) {
      headers.set('Authorization', `Bearer ${token}`)
    } else if (this.config.dev_identity && ['development', 'simulator', 'test'].includes(this.config.environment)) {
      headers.set('X-Tenant-Id', this.config.dev_identity.tenant_id)
      headers.set('X-Actor-Id', this.config.dev_identity.actor_id)
      headers.set('X-Roles', this.config.dev_identity.roles)
    }
    if (init.body) headers.set('Content-Type', 'application/json')
    if (init.method && !['GET', 'HEAD'].includes(init.method.toUpperCase())) {
      headers.set('Idempotency-Key', controls.idempotencyKey || crypto.randomUUID())
    }
    if (controls.ifMatch) headers.set('If-Match', controls.ifMatch)
    if (controls.correlationId) headers.set('X-Correlation-Id', controls.correlationId)
    const response = await fetch(path, { ...init, headers, credentials: 'same-origin', redirect: 'error' })
    const contentType = response.headers.get('Content-Type') || ''
    const body = contentType.includes('application/json') ? await response.json() as unknown : await response.text()
    if (!response.ok) {
      let message = response.statusText
      if (typeof body === 'object' && body) {
        if ('message' in body) message = String((body as { message?: unknown }).message || response.statusText)
        else if ('error' in body) {
          const error = (body as { error?: unknown }).error
          message = typeof error === 'object' && error && 'message' in error
            ? String((error as { message?: unknown }).message || response.statusText)
            : String(error || response.statusText)
        }
        else if ('detail' in body) message = String((body as { detail?: unknown }).detail || response.statusText)
      } else if (body) message = String(body)
      throw new ApiError(response.status, message, response.headers.get('X-Correlation-Id') || undefined)
    }
    return {
      data: body,
      status: response.status,
      etag: response.headers.get('ETag'),
      correlationId: response.headers.get('X-Correlation-Id'),
    }
  }

  async request(path: string, init: RequestInit = {}, controls: RequestControls = {}): Promise<unknown> {
    return (await this.requestDetailed(path, init, controls)).data
  }
}
