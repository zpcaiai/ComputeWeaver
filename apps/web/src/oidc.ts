export type RuntimeConfig = {
  environment: string
  oidc: { issuer: string | null; client_id: string | null; audience: string | null; scopes: string }
  dev_identity: { tenant_id: string; actor_id: string; roles: string } | null
  release_id: string
  release_commit: string | null
}

type Discovery = {
  issuer: string
  authorization_endpoint: string
  token_endpoint: string
}

type AuthSession = { accessToken: string; expiresAt: number }

const SESSION_KEY = 'computeweaver.auth'
const FLOW_KEY = 'computeweaver.oidc.flow'

function base64Url(bytes: Uint8Array): string {
  let binary = ''
  bytes.forEach((value) => { binary += String.fromCharCode(value) })
  return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '')
}

function randomValue(bytes = 32): string {
  return base64Url(crypto.getRandomValues(new Uint8Array(bytes)))
}

async function discover(issuer: string): Promise<Discovery> {
  const normalized = issuer.replace(/\/$/, '')
  const response = await fetch(`${normalized}/.well-known/openid-configuration`, {
    credentials: 'omit', cache: 'no-store', redirect: 'error'
  })
  if (!response.ok) throw new Error('OIDC discovery failed')
  const document = await response.json() as Partial<Discovery>
  if (document.issuer?.replace(/\/$/, '') !== normalized) throw new Error('OIDC issuer mismatch')
  if (!document.authorization_endpoint?.startsWith('https://') || !document.token_endpoint?.startsWith('https://')) {
    throw new Error('OIDC endpoints must use HTTPS')
  }
  return document as Discovery
}

function redirectUri(): string {
  return `${window.location.origin}${window.location.pathname}`
}

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  const response = await fetch('/web-config.json', { cache: 'no-store' })
  if (!response.ok) throw new Error('Web runtime configuration is unavailable')
  return await response.json() as RuntimeConfig
}

export function currentAccessToken(): string | null {
  const raw = sessionStorage.getItem(SESSION_KEY)
  if (!raw) return null
  try {
    const session = JSON.parse(raw) as AuthSession
    if (!session.accessToken || Date.now() >= session.expiresAt - 30_000) {
      sessionStorage.removeItem(SESSION_KEY)
      return null
    }
    return session.accessToken
  } catch {
    sessionStorage.removeItem(SESSION_KEY)
    return null
  }
}

export async function beginLogin(config: RuntimeConfig): Promise<void> {
  const { issuer, client_id: clientId, audience, scopes } = config.oidc
  if (!issuer || !clientId) throw new Error('OIDC login is not configured')
  const metadata = await discover(issuer)
  const verifier = randomValue(48)
  const challenge = base64Url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))))
  const state = randomValue()
  const nonce = randomValue()
  sessionStorage.setItem(FLOW_KEY, JSON.stringify({ verifier, state, nonce }))
  const query = new URLSearchParams({
    response_type: 'code', client_id: clientId, redirect_uri: redirectUri(), scope: scopes,
    state, nonce, code_challenge: challenge, code_challenge_method: 'S256'
  })
  if (audience) query.set('audience', audience)
  window.location.assign(`${metadata.authorization_endpoint}?${query}`)
}

export async function completeLogin(config: RuntimeConfig): Promise<boolean> {
  const query = new URLSearchParams(window.location.search)
  if (!query.has('code') && !query.has('error')) return false
  const flowRaw = sessionStorage.getItem(FLOW_KEY)
  sessionStorage.removeItem(FLOW_KEY)
  history.replaceState({}, document.title, window.location.pathname)
  if (query.has('error')) throw new Error(query.get('error_description') || query.get('error') || 'OIDC login failed')
  if (!flowRaw) throw new Error('OIDC login state is missing')
  const flow = JSON.parse(flowRaw) as { verifier: string; state: string; nonce: string }
  if (!query.get('state') || query.get('state') !== flow.state) throw new Error('OIDC state validation failed')
  const { issuer, client_id: clientId } = config.oidc
  if (!issuer || !clientId) throw new Error('OIDC login is not configured')
  const metadata = await discover(issuer)
  const response = await fetch(metadata.token_endpoint, {
    method: 'POST', credentials: 'omit', redirect: 'error',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code', code: query.get('code') || '', redirect_uri: redirectUri(),
      client_id: clientId, code_verifier: flow.verifier
    })
  })
  if (!response.ok) throw new Error('OIDC token exchange failed')
  const token = await response.json() as { access_token?: unknown; token_type?: unknown; expires_in?: unknown }
  if (typeof token.access_token !== 'string' || String(token.token_type).toLowerCase() !== 'bearer') {
    throw new Error('OIDC token response is invalid')
  }
  const expiresIn = Number(token.expires_in)
  if (!Number.isFinite(expiresIn) || expiresIn <= 0) throw new Error('OIDC token expiry is invalid')
  sessionStorage.setItem(SESSION_KEY, JSON.stringify({ accessToken: token.access_token, expiresAt: Date.now() + expiresIn * 1000 }))
  return true
}

export function signOut(): void {
  sessionStorage.removeItem(SESSION_KEY)
  sessionStorage.removeItem(FLOW_KEY)
}
