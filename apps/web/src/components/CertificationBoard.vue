<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import type { ApiTransport } from '../api'
import ResultView from './ResultView.vue'

const props = defineProps<{ client: ApiTransport; releaseId: string }>()
const emit = defineEmits<{ context: [value: { sourceRevision?: string; certificateHash?: string }] }>()
const certificate = ref<unknown>(null)
const readiness = ref<unknown>(null)
const events = ref<unknown>(null)
const loading = ref(false)
const error = ref('')

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  const release = encodeURIComponent(props.releaseId)
  const results = await Promise.allSettled([
    props.client.request(`/v1/certification/${release}`),
    props.client.request(`/v1/certification/${release}/external-readiness`),
    props.client.request(`/v1/certification/${release}/events`),
  ])
  certificate.value = results[0].status === 'fulfilled' ? results[0].value : null
  readiness.value = results[1].status === 'fulfilled' ? results[1].value : null
  events.value = results[2].status === 'fulfilled' ? results[2].value : null
  const rejected = results.find((item) => item.status === 'rejected')
  if (rejected?.status === 'rejected') error.value = rejected.reason instanceof Error ? rejected.reason.message : String(rejected.reason)
  if (certificate.value && typeof certificate.value === 'object') {
    const document = certificate.value as Record<string, unknown>
    emit('context', {
      sourceRevision: typeof document.commit === 'string' ? document.commit : undefined,
      certificateHash: typeof document.certificate_hash === 'string' ? document.certificate_hash : undefined,
    })
  }
  loading.value = false
}

onMounted(load)
watch(() => props.releaseId, load)
defineExpose({ load })
</script>

<template>
  <section class="certification-board" aria-labelledby="certification-title">
    <div class="board-heading">
      <div><p class="kicker">Immutable release evidence</p><h3 id="certification-title">Production certification</h3></div>
      <button class="quiet" :disabled="loading" @click="load">{{ loading ? 'Refreshing…' : 'Refresh gates' }}</button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div class="board-grid">
      <article><h4>Certificate</h4><ResultView v-if="certificate" :data="certificate" presentation="certification" /></article>
      <article><h4>External readiness</h4><ResultView v-if="readiness" :data="readiness" presentation="certification" /></article>
      <article><h4>Lifecycle event chain</h4><ResultView v-if="events" :data="events" presentation="timeline" /></article>
    </div>
  </section>
</template>
