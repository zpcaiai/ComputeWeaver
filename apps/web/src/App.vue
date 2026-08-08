<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ApiClient } from './api'
import { beginLogin, completeLogin, currentAccessToken, loadRuntimeConfig, signOut, type RuntimeConfig } from './oidc'

type Panel = { label: string; endpoint: string }
type PanelState = Panel & { loading: boolean; data?: unknown; error?: string }
type Gate = { name: string; passed: boolean; reason?: string | null; evidence?: string[] }
type CertificationView = {
  release_id: string; commit: string; status: string; generated_at: string; expires_at: string
  gates: Gate[]; risks: string[]; artifacts: Record<string, string>; approvals: Array<Record<string, unknown>>
  scenario_metrics: Record<string, unknown>; test_summary: Record<string, unknown>; revocation?: Record<string, unknown>
}

const sections: Record<string, Panel[]> = {
  overview: [
    { label: 'Release gate', endpoint: '/v1/certification/local' },
    { label: 'Audit integrity', endpoint: '/v1/audit/integrity' },
    { label: 'Data quality', endpoint: '/v1/data-quality/status' },
  ],
  topology: [
    { label: 'Assets', endpoint: '/v1/assets' },
    { label: 'Topology versions', endpoint: '/v1/topology/versions' },
    { label: 'Topology graph', endpoint: '/v1/topology/graph' },
  ],
  compute: [
    { label: 'Compute nodes', endpoint: '/v1/compute/nodes' },
    { label: 'GPUs', endpoint: '/v1/compute/gpus' },
    { label: 'Reservations', endpoint: '/v1/compute/reservations' },
  ],
  workloads: [
    { label: 'Jobs', endpoint: '/v1/jobs' },
    { label: 'Quota usage', endpoint: '/v1/quotas/current' },
    { label: 'Reservations', endpoint: '/v1/reservations' },
  ],
  energy: [
    { label: 'Energy assets', endpoint: '/v1/energy/assets' },
    { label: 'Energy state', endpoint: '/v1/energy/state' },
    { label: 'Tariffs', endpoint: '/v1/tariffs' },
  ],
  governance: [
    { label: 'Approvals', endpoint: '/v1/approvals' },
    { label: 'Chargeback', endpoint: '/v1/chargeback' },
    { label: 'Notification routes', endpoint: '/v1/notifications/routes' },
  ],
  admin: [
    { label: 'Configuration', endpoint: '/v1/admin/config' },
    { label: 'Connectors', endpoint: '/v1/admin/connectors' },
    { label: 'Models', endpoint: '/v1/admin/models' },
    { label: 'Solvers', endpoint: '/v1/admin/solvers' },
  ],
}

const operations = {
  simulation: {
    label: 'Create deterministic simulation', endpoint: '/v1/simulations',
    body: { id: 'simulation-console', data: { seed: 7, duration_hours: 24, step_minutes: 15, gpu_count: 16 } }
  },
  job: { label: 'Create workload record', endpoint: '/v1/jobs', body: { id: 'job-console', data: { priority: 50 } } },
  config: {
    label: 'Create versioned configuration', endpoint: '/v1/admin/config',
    body: { id: 'runtime-console', data: { mode: 'safe', max_parallel: 2 } }
  },
}

const health = ref<Record<string, unknown>>({ status: 'connecting' })
const version = ref('unknown')
const runtime = ref<RuntimeConfig | null>(null)
const client = ref<ApiClient | null>(null)
const active = ref('overview')
const panels = ref<PanelState[]>([])
const authError = ref('')
const operationKey = ref<keyof typeof operations>('simulation')
const operationBody = ref(JSON.stringify(operations.simulation.body, null, 2))
const operationResult = ref<unknown>(null)
const operationError = ref('')
const operationBusy = ref(false)

const authenticated = computed(() => client.value?.isAuthenticated() || false)
const authLabel = computed(() => runtime.value?.dev_identity && !currentAccessToken() ? 'local simulator identity' : 'OIDC session')

async function loadPanels(): Promise<void> {
  if (!client.value || !authenticated.value) return
  panels.value = (sections[active.value] || []).map((panel) => ({ ...panel, loading: true }))
  await Promise.all(panels.value.map(async (panel) => {
    try { panel.data = await client.value?.request(panel.endpoint) }
    catch (error) { panel.error = error instanceof Error ? error.message : String(error) }
    finally { panel.loading = false }
  }))
}

async function selectSection(name: string): Promise<void> {
  active.value = name
  await loadPanels()
}

async function login(): Promise<void> {
  if (!runtime.value) return
  authError.value = ''
  try { await beginLogin(runtime.value) }
  catch (error) { authError.value = error instanceof Error ? error.message : String(error) }
}

function logout(): void {
  signOut()
  client.value = runtime.value ? new ApiClient(runtime.value) : null
  panels.value = []
}

function chooseOperation(): void {
  operationBody.value = JSON.stringify(operations[operationKey.value].body, null, 2)
  operationResult.value = null
  operationError.value = ''
}

async function runOperation(): Promise<void> {
  if (!client.value) return
  operationBusy.value = true
  operationError.value = ''
  operationResult.value = null
  try {
    operationResult.value = await client.value.request(operations[operationKey.value].endpoint, {
      method: 'POST', body: JSON.stringify(JSON.parse(operationBody.value) as unknown)
    })
    await loadPanels()
  } catch (error) {
    operationError.value = error instanceof Error ? error.message : String(error)
  } finally { operationBusy.value = false }
}

function certificationData(panel: PanelState): CertificationView | null {
  if (!panel.endpoint.startsWith('/v1/certification/') || !panel.data || typeof panel.data !== 'object') return null
  return panel.data as CertificationView
}

function formatted(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

onMounted(async () => {
  try {
    const ready = await fetch('/health/ready', { cache: 'no-store' })
    health.value = await ready.json() as Record<string, unknown>
    const build = await fetch('/version', { cache: 'no-store' })
    version.value = String((await build.json() as { version: string }).version)
    runtime.value = await loadRuntimeConfig()
    sections.overview[0].endpoint = `/v1/certification/${encodeURIComponent(runtime.value.release_id)}`
    await completeLogin(runtime.value)
    client.value = new ApiClient(runtime.value)
    await loadPanels()
  } catch (error) {
    authError.value = error instanceof Error ? error.message : String(error)
    if (health.value.status === 'connecting') health.value = { status: 'offline' }
  }
})
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div><p class="eyebrow">COMPUTEWEAVER</p><h1>Compute + energy control plane</h1></div>
      <div class="top-actions">
        <span :class="['status', health.status]">{{ health.status }}</span>
        <span class="version">v{{ version }}</span>
        <button v-if="!authenticated" class="primary" @click="login">Sign in</button>
        <button v-else class="quiet" @click="logout">Sign out</button>
      </div>
    </header>

    <div v-if="authError" class="alert" role="alert">{{ authError }}</div>
    <section v-if="!authenticated" class="welcome">
      <p class="kicker">Governed infrastructure operations</p>
      <h2>One operational surface for capacity, power, policy and evidence.</h2>
      <p>Authenticate through the configured OIDC provider. Production mode never accepts browser-supplied trusted identity headers.</p>
      <button class="primary" @click="login">Continue with identity provider</button>
    </section>

    <template v-else>
      <aside>
        <p class="session">Authenticated via<br><strong>{{ authLabel }}</strong></p>
        <nav aria-label="Control plane areas">
          <button v-for="(_, name) in sections" :key="name" :class="{ active: active === name }" @click="selectSection(name)">
            {{ name }}
          </button>
        </nav>
      </aside>

      <main>
        <div class="section-heading"><p class="kicker">{{ active }}</p><h2>Operational state</h2>
          <button class="quiet" @click="loadPanels">Refresh</button></div>
        <section class="panel-grid">
          <article v-for="panel in panels" :key="panel.endpoint" :class="['panel', { 'release-panel': certificationData(panel) }]">
            <div class="panel-title"><h3>{{ panel.label }}</h3><code>{{ panel.endpoint }}</code></div>
            <p v-if="panel.loading" class="muted">Loading…</p>
            <p v-else-if="panel.error" class="error">{{ panel.error }}</p>
            <div v-else-if="certificationData(panel)" class="release-readiness">
              <div class="release-summary">
                <span :class="['gate-state', certificationData(panel)?.status.toLowerCase()]">{{ certificationData(panel)?.status }}</span>
                <div><strong>{{ certificationData(panel)?.release_id }}</strong><small>{{ certificationData(panel)?.commit }}</small></div>
                <div><small>Expires</small><strong>{{ certificationData(panel)?.expires_at }}</strong></div>
              </div>
              <p v-if="certificationData(panel)?.revocation" class="revoked">This certificate has been revoked.</p>
              <table>
                <thead><tr><th>Mandatory gate</th><th>State</th><th>Evidence or blocker</th></tr></thead>
                <tbody><tr v-for="gate in certificationData(panel)?.gates" :key="gate.name">
                  <td>{{ gate.name }}</td><td><span :class="['gate-dot', { pass: gate.passed }]">{{ gate.passed ? 'PASS' : 'BLOCKED' }}</span></td>
                  <td>{{ gate.passed ? `${gate.evidence?.length || 0} bound artifact(s)` : gate.reason }}</td>
                </tr></tbody>
              </table>
              <div class="release-facts">
                <span>Artifacts <strong>{{ Object.keys(certificationData(panel)?.artifacts || {}).length }}</strong></span>
                <span>Approvals <strong>{{ certificationData(panel)?.approvals?.length || 0 }}</strong></span>
                <span>Risks <strong>{{ certificationData(panel)?.risks?.length || 0 }}</strong></span>
              </div>
            </div>
            <pre v-else>{{ formatted(panel.data) }}</pre>
          </article>
        </section>

        <section class="operation">
          <div><p class="kicker">Controlled write</p><h2>Validated API operation</h2>
            <p>Requests receive a unique idempotency key and remain subject to RBAC, policy, audit and approval gates.</p></div>
          <form @submit.prevent="runOperation">
            <label>Operation<select v-model="operationKey" @change="chooseOperation">
              <option v-for="(item, key) in operations" :key="key" :value="key">{{ item.label }}</option>
            </select></label>
            <label>Validated JSON payload<textarea v-model="operationBody" rows="12" spellcheck="false" /></label>
            <button class="primary" :disabled="operationBusy">{{ operationBusy ? 'Submitting…' : 'Submit operation' }}</button>
            <p v-if="operationError" class="error" role="alert">{{ operationError }}</p>
            <pre v-if="operationResult">{{ formatted(operationResult) }}</pre>
          </form>
        </section>
      </main>
    </template>
  </div>
</template>
