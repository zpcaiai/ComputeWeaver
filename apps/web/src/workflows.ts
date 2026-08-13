import catalogDocument from './workflows.generated.json'

export type WorkflowParameter = {
  name: string
  location: 'path' | 'query'
  required: boolean
  default: string
}

export type WorkflowOperation = {
  id: string
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  path: string
  summary: string
  risk: 'read' | 'controlled' | 'standard' | 'high'
  presentation: 'table' | 'graph' | 'timeline' | 'metrics' | 'certification'
  parameters: WorkflowParameter[]
  required_headers: string[]
  body_example: unknown | null
  has_body: boolean
  requires_confirmation: boolean
  audited: boolean
  compensation: string
}

export type SkillWorkflow = {
  id: string
  title: string
  mission: string
  operations: WorkflowOperation[]
}

export type WorkflowContext = {
  releaseId: string
  sourceRevision?: string
  certificateHash?: string
}

type WorkflowCatalog = {
  schema_version: string
  source: string
  operation_count: number
  skill_count: number
  unmapped_operations: string[]
  skills: SkillWorkflow[]
}

export const workflowCatalog = catalogDocument as WorkflowCatalog

export function materializeTokens<T>(value: T, context: WorkflowContext): T {
  if (typeof value === 'string') {
    const replacements: Record<string, string> = {
      '$release_id': context.releaseId,
      '$source_revision': context.sourceRevision || 'REPLACE_WITH_IMMUTABLE_SOURCE_REVISION',
      '$certificate_hash': context.certificateHash || 'REPLACE_WITH_CERTIFICATE_HASH',
    }
    return (replacements[value] ?? value) as T
  }
  if (Array.isArray(value)) return value.map((item) => materializeTokens(item, context)) as T
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [key, materializeTokens(item, context)]),
    ) as T
  }
  return value
}

export function initialParameters(operation: WorkflowOperation, context: WorkflowContext): Record<string, string> {
  return Object.fromEntries(
    operation.parameters.map((parameter) => [parameter.name, materializeTokens(parameter.default, context)]),
  )
}

export function buildOperationPath(operation: WorkflowOperation, values: Record<string, string>): string {
  let path = operation.path
  const query = new URLSearchParams()
  for (const parameter of operation.parameters) {
    const value = String(values[parameter.name] || '').trim()
    if (parameter.required && !value) throw new Error(`${parameter.name} is required`)
    if (!value) continue
    if (parameter.location === 'path') {
      path = path.replace(`{${parameter.name}}`, encodeURIComponent(value))
    } else {
      query.set(parameter.name, value)
    }
  }
  if (/\{[^}]+\}/.test(path)) throw new Error('all path parameters must be supplied')
  const rendered = query.toString()
  return rendered ? `${path}?${rendered}` : path
}

export function operationBody(operation: WorkflowOperation, bodyText: string): string | undefined {
  if (!operation.has_body) return undefined
  const parsed = JSON.parse(bodyText) as unknown
  if (parsed === null || (typeof parsed !== 'object' && typeof parsed !== 'string')) {
    throw new Error('request body must be a JSON object or string')
  }
  return JSON.stringify(parsed)
}

export function validateCatalog(): void {
  if (workflowCatalog.skill_count !== 20 || workflowCatalog.skills.length !== 20) {
    throw new Error('workflow catalog must cover B01 through B20')
  }
  const operations = workflowCatalog.skills.flatMap((skill) => skill.operations)
  if (operations.length !== workflowCatalog.operation_count || workflowCatalog.unmapped_operations.length) {
    throw new Error('workflow catalog does not cover every OpenAPI operation')
  }
  if (new Set(operations.map((operation) => operation.id)).size !== operations.length) {
    throw new Error('workflow catalog contains duplicate operations')
  }
}

validateCatalog()
