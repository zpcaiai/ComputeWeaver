<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import type { ApiResponse, ApiTransport } from '../api'
import type { RuntimeConfig } from '../oidc'
import {
  buildOperationPath,
  initialParameters,
  materializeTokens,
  operationBody,
  type SkillWorkflow,
  type WorkflowContext,
  type WorkflowOperation,
} from '../workflows'
import CertificationBoard from './CertificationBoard.vue'
import ResultView from './ResultView.vue'

const props = defineProps<{ skill: SkillWorkflow; client: ApiTransport; runtime: RuntimeConfig }>()
type RunRecord = { at: string; operation: string; path: string; status: 'PASS' | 'FAIL'; correlationId?: string | null }

const selectedId = ref('')
const parameterValues = ref<Record<string, string>>({})
const bodyText = ref('')
const ifMatch = ref('')
const idempotencyKey = ref(crypto.randomUUID())
const correlationId = ref(crypto.randomUUID())
const confirmation = ref(false)
const busy = ref(false)
const result = ref<unknown>(null)
const error = ref('')
const responseMeta = ref<ApiResponse | null>(null)
const history = ref<RunRecord[]>([])
const contract = ref<unknown>(null)
const certificationContext = ref<{ sourceRevision?: string; certificateHash?: string }>({})

const context = computed<WorkflowContext>(() => ({
  releaseId: props.runtime.release_id,
  sourceRevision: certificationContext.value.sourceRevision,
  certificateHash: certificationContext.value.certificateHash,
}))
const selected = computed(() => props.skill.operations.find((operation) => operation.id === selectedId.value) || null)
const reads = computed(() => props.skill.operations.filter((operation) => operation.method === 'GET').length)
const writes = computed(() => props.skill.operations.length - reads.value)

function resetOperation(operation: WorkflowOperation | null): void {
  result.value = null
  responseMeta.value = null
  error.value = ''
  confirmation.value = false
  ifMatch.value = ''
  idempotencyKey.value = crypto.randomUUID()
  correlationId.value = crypto.randomUUID()
  if (!operation) {
    parameterValues.value = {}
    bodyText.value = ''
    return
  }
  parameterValues.value = initialParameters(operation, context.value)
  const body = materializeTokens(operation.body_example, context.value)
  bodyText.value = operation.has_body ? JSON.stringify(body, null, 2) : ''
}

function selectOperation(): void {
  resetOperation(selected.value)
}

async function execute(): Promise<void> {
  const operation = selected.value
  if (!operation) return
  if (operation.requires_confirmation && !confirmation.value) {
    error.value = 'A high-risk operation requires explicit confirmation.'
    return
  }
  busy.value = true
  error.value = ''
  result.value = null
  try {
    const path = buildOperationPath(operation, parameterValues.value)
    const body = operationBody(operation, bodyText.value)
    const response = await props.client.requestDetailed(path, {
      method: operation.method,
      body,
    }, {
      idempotencyKey: idempotencyKey.value,
      ifMatch: ifMatch.value || undefined,
      correlationId: correlationId.value,
    })
    responseMeta.value = response
    result.value = response.data
    history.value.unshift({
      at: new Date().toISOString(), operation: operation.summary, path,
      status: 'PASS', correlationId: response.correlationId || correlationId.value,
    })
    history.value = history.value.slice(0, 20)
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : String(caught)
    history.value.unshift({
      at: new Date().toISOString(), operation: operation.summary, path: operation.path,
      status: 'FAIL', correlationId: correlationId.value,
    })
  } finally {
    busy.value = false
  }
}

async function loadContractRegistry(): Promise<void> {
  if (props.skill.id !== 'B02') return
  try { contract.value = await props.client.request('/openapi.json') }
  catch (caught) { error.value = caught instanceof Error ? caught.message : String(caught) }
}

function updateCertificationContext(value: { sourceRevision?: string; certificateHash?: string }): void {
  certificationContext.value = value
  resetOperation(selected.value)
}

function initialize(): void {
  selectedId.value = props.skill.operations[0]?.id || ''
  resetOperation(selected.value)
  void loadContractRegistry()
}

onMounted(initialize)
watch(() => props.skill.id, initialize)
</script>

<template>
  <div class="skill-workspace">
    <header class="skill-heading">
      <div><p class="kicker">{{ skill.id }} · governed workspace</p><h2>{{ skill.title }}</h2><p>{{ skill.mission }}</p></div>
      <div class="coverage-facts">
        <span><strong>{{ skill.operations.length }}</strong> API operations</span>
        <span><strong>{{ reads }}</strong> reads</span>
        <span><strong>{{ writes }}</strong> controlled writes</span>
      </div>
    </header>

    <CertificationBoard
      v-if="skill.id === 'B20'"
      :client="client"
      :release-id="runtime.release_id"
      @context="updateCertificationContext"
    />

    <section v-if="skill.id === 'B02'" class="contract-registry">
      <div class="board-heading"><div><p class="kicker">Generated source of truth</p><h3>Contract registry</h3></div>
        <a class="button-link" href="/openapi.json" target="_blank" rel="noreferrer">Download OpenAPI</a></div>
      <p>Every operation shown in this console is generated from this contract. Contract drift or an unmapped operation fails the repository build.</p>
      <ResultView v-if="contract" :data="contract" presentation="table" />
    </section>

    <section v-if="skill.operations.length" class="workflow-grid">
      <aside class="operation-index" aria-label="Skill operations">
        <label class="search-label">Operation
          <select v-model="selectedId" @change="selectOperation">
            <option v-for="operation in skill.operations" :key="operation.id" :value="operation.id">
              {{ operation.method }} · {{ operation.summary }}
            </option>
          </select>
        </label>
        <button
          v-for="operation in skill.operations"
          :key="operation.id"
          :class="['operation-link', { active: selectedId === operation.id }]"
          @click="selectedId = operation.id; selectOperation()"
        >
          <span :class="['method', operation.method.toLowerCase()]">{{ operation.method }}</span>
          <span>{{ operation.summary }}<small>{{ operation.path }}</small></span>
        </button>
      </aside>

      <section v-if="selected" class="operation-console">
        <div class="operation-title">
          <div><p class="kicker">{{ selected.risk }} risk · {{ selected.audited ? 'audited' : 'read only' }}</p><h3>{{ selected.summary }}</h3><code>{{ selected.method }} {{ selected.path }}</code></div>
          <span :class="['risk-badge', selected.risk]">{{ selected.risk }}</span>
        </div>

        <form @submit.prevent="execute">
          <fieldset v-if="selected.parameters.length">
            <legend>Path and query parameters</legend>
            <label v-for="parameter in selected.parameters" :key="parameter.name">
              {{ parameter.name }} <small>{{ parameter.location }}{{ parameter.required ? ' · required' : '' }}</small>
              <input v-model="parameterValues[parameter.name]" :required="parameter.required" />
            </label>
          </fieldset>

          <fieldset v-if="selected.method !== 'GET'">
            <legend>Request controls</legend>
            <label>Idempotency key <small>retained for safe retry</small><input v-model="idempotencyKey" required minlength="8" /></label>
            <label>Correlation ID <small>audit and trace linkage</small><input v-model="correlationId" required /></label>
            <label>If-Match <small>optional optimistic concurrency ETag</small><input v-model="ifMatch" placeholder="Current resource ETag" /></label>
          </fieldset>

          <label v-if="selected.has_body" class="body-editor">Validated JSON payload
            <textarea v-model="bodyText" rows="14" spellcheck="false" />
          </label>

          <div class="safety-contract">
            <span><strong>Authorization</strong> OIDC/RBAC and tenant scope are enforced server-side.</span>
            <span><strong>Audit</strong> {{ selected.audited ? 'Intent and outcome are append-only audited.' : 'No state change is performed.' }}</span>
            <span><strong>Recovery</strong> {{ selected.compensation }}</span>
          </div>

          <label v-if="selected.requires_confirmation" class="confirmation">
            <input v-model="confirmation" type="checkbox" />
            I reviewed the target, current state, approval evidence and compensation path.
          </label>

          <button class="primary execute" :disabled="busy">{{ busy ? 'Executing…' : selected.method === 'GET' ? 'Run query' : 'Submit governed operation' }}</button>
          <p v-if="error" class="error" role="alert">{{ error }}</p>
        </form>

        <section v-if="result !== null" class="operation-result" aria-live="polite">
          <div class="result-meta"><strong>Operation completed</strong><span>HTTP {{ responseMeta?.status }}</span><span>ETag {{ responseMeta?.etag || 'not returned' }}</span></div>
          <ResultView :data="result" :presentation="selected.presentation" />
        </section>

        <section v-if="history.length" class="operation-history">
          <h4>Session operation history</h4>
          <ol><li v-for="entry in history" :key="`${entry.at}-${entry.correlationId}`">
            <span :class="['run-status', entry.status.toLowerCase()]">{{ entry.status }}</span>
            <strong>{{ entry.operation }}</strong><code>{{ entry.path }}</code><small>{{ entry.at }} · {{ entry.correlationId }}</small>
          </li></ol>
        </section>
      </section>
    </section>
  </div>
</template>
