import { currentAccessToken, type RuntimeConfig } from './oidc'

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

export class ApiClient {
  constructor(private config: RuntimeConfig) {}

  isAuthenticated(): boolean {
    return Boolean(currentAccessToken() || this.config.dev_identity)
  }

  async request(path: string, init: RequestInit = {}): Promise<unknown> {
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
      headers.set('Idempotency-Key', crypto.randomUUID())
    }
    const response = await fetch(path, { ...init, headers, credentials: 'same-origin', redirect: 'error' })
    const contentType = response.headers.get('Content-Type') || ''
    const body = contentType.includes('application/json') ? await response.json() as unknown : await response.text()
    if (!response.ok) {
      let message = response.statusText
      if (typeof body === 'object' && body) {
        if ('message' in body) message = String((body as { message?: unknown }).message || response.statusText)
        else if ('error' in body) message = String((body as { error?: unknown }).error || response.statusText)
      } else if (body) message = String(body)
      throw new ApiError(response.status, message)
    }
    return body
  }
}
