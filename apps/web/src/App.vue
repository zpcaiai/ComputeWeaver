<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ApiClient } from './api'
import WorkflowWorkspace from './components/WorkflowWorkspace.vue'
import { beginLogin, completeLogin, currentAccessToken, loadRuntimeConfig, signOut, type RuntimeConfig } from './oidc'
import { workflowCatalog } from './workflows'

const health = ref<Record<string, unknown>>({ status: 'connecting' })
const version = ref('unknown')
const runtime = ref<RuntimeConfig | null>(null)
const client = ref<ApiClient | null>(null)
const activeSkillId = ref('B01')
const search = ref('')
const authError = ref('')

const authenticated = computed(() => client.value?.isAuthenticated() || false)
const authLabel = computed(() => runtime.value?.dev_identity && !currentAccessToken() ? 'local simulator identity' : 'OIDC session')
const activeSkill = computed(() => workflowCatalog.skills.find((skill) => skill.id === activeSkillId.value) || workflowCatalog.skills[0])
const filteredSkills = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return workflowCatalog.skills
  return workflowCatalog.skills.filter((skill) => `${skill.id} ${skill.title} ${skill.mission}`.toLowerCase().includes(query))
})

async function login(): Promise<void> {
  if (!runtime.value) return
  authError.value = ''
  try { await beginLogin(runtime.value) }
  catch (error) { authError.value = error instanceof Error ? error.message : String(error) }
}

function logout(): void {
  signOut()
  client.value = runtime.value ? new ApiClient(runtime.value) : null
}

function chooseSkill(id: string): void {
  activeSkillId.value = id
  document.querySelector('#workspace')?.scrollIntoView({ block: 'start' })
}

onMounted(async () => {
  try {
    const ready = await fetch('/health/ready', { cache: 'no-store' })
    health.value = await ready.json() as Record<string, unknown>
    const build = await fetch('/version', { cache: 'no-store' })
    version.value = String((await build.json() as { version: string }).version)
    runtime.value = await loadRuntimeConfig()
    await completeLogin(runtime.value)
    client.value = new ApiClient(runtime.value)
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
      <div class="catalog-proof" aria-label="Workflow coverage">
        <span><strong>{{ workflowCatalog.skill_count }}</strong> skills</span>
        <span><strong>{{ workflowCatalog.operation_count }}</strong> API operations</span>
        <span><strong>{{ workflowCatalog.unmapped_operations.length }}</strong> unmapped</span>
      </div>
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
      <h2>One operational surface for every compute-energy workflow.</h2>
      <p>Authenticate through the configured OIDC provider. Production mode never accepts browser-supplied trusted identity headers.</p>
      <button class="primary" @click="login">Continue with identity provider</button>
    </section>

    <template v-else-if="runtime && client">
      <aside class="skill-navigation">
        <p class="session">Authenticated via<br><strong>{{ authLabel }}</strong><br><small>Release {{ runtime.release_id }}</small></p>
        <label class="nav-search"><span>Find a skill</span><input v-model="search" type="search" placeholder="B16 or approvals" /></label>
        <nav aria-label="B01 through B20 skill workspaces">
          <button
            v-for="skill in filteredSkills"
            :key="skill.id"
            :class="{ active: activeSkillId === skill.id }"
            @click="chooseSkill(skill.id)"
          >
            <strong>{{ skill.id }}</strong><span>{{ skill.title }}</span><small>{{ skill.operations.length }}</small>
          </button>
        </nav>
      </aside>

      <main id="workspace" class="workspace">
        <WorkflowWorkspace :key="activeSkill.id" :skill="activeSkill" :client="client" :runtime="runtime" />
      </main>
    </template>
  </div>
</template>
