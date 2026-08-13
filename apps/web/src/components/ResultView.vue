<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ data: unknown; presentation?: string }>()

const objectData = computed<Record<string, unknown> | null>(() => (
  props.data && typeof props.data === 'object' && !Array.isArray(props.data)
    ? props.data as Record<string, unknown>
    : null
))
const rows = computed<unknown[]>(() => {
  if (Array.isArray(props.data)) return props.data
  const candidate = objectData.value
  if (!candidate) return []
  for (const key of ['items', 'records', 'nodes', 'events', 'checks', 'gates', 'results']) {
    if (Array.isArray(candidate[key])) return candidate[key] as unknown[]
  }
  return []
})
const columns = computed(() => {
  const names = new Set<string>()
  rows.value.slice(0, 25).forEach((row) => {
    if (row && typeof row === 'object') Object.keys(row as Record<string, unknown>).forEach((key) => names.add(key))
  })
  return [...names].slice(0, 8)
})
const scalarFacts = computed(() => Object.entries(objectData.value || {}).filter(([, value]) => (
  value === null || ['string', 'number', 'boolean'].includes(typeof value)
)).slice(0, 16))

function display(value: unknown): string {
  if (value === null || value === undefined) return '—'
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

function cell(row: unknown, column: string): unknown {
  return row && typeof row === 'object' ? (row as Record<string, unknown>)[column] : row
}

function statusClass(value: unknown): string {
  const normalized = String(value || '').toLowerCase()
  return ['fail', 'blocked', 'not_certified', 'revoked'].some((part) => normalized.includes(part)) ? 'fail' :
    ['pass', 'ready', 'certified', 'complete'].some((part) => normalized.includes(part)) ? 'pass' : 'neutral'
}
</script>

<template>
  <div class="result-view">
    <div v-if="scalarFacts.length" class="fact-grid">
      <div v-for="([key, value]) in scalarFacts" :key="key" class="fact">
        <span>{{ key.replaceAll('_', ' ') }}</span>
        <strong :class="key.includes('status') || key === 'integrity' ? statusClass(value) : ''">{{ display(value) }}</strong>
      </div>
    </div>

    <div v-if="rows.length" :class="['structured-result', presentation]">
      <table>
        <thead><tr><th v-for="column in columns" :key="column">{{ column.replaceAll('_', ' ') }}</th></tr></thead>
        <tbody>
          <tr v-for="(row, index) in rows.slice(0, 100)" :key="index">
            <td v-for="column in columns" :key="column">
              <span :class="column === 'status' || column === 'passed' ? statusClass(cell(row, column)) : ''">
                {{ display(cell(row, column)) }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="rows.length > 100" class="muted">Showing the first 100 of {{ rows.length }} records.</p>
    </div>

    <details class="raw-result">
      <summary>Machine-readable response</summary>
      <pre>{{ JSON.stringify(data, null, 2) }}</pre>
    </details>
  </div>
</template>
