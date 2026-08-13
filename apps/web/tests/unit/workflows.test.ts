import { describe, expect, it } from 'vitest'
import {
  buildOperationPath,
  initialParameters,
  materializeTokens,
  operationBody,
  validateCatalog,
  workflowCatalog,
} from '../../src/workflows'

describe('generated workflow catalog', () => {
  it('covers B01 through B20 and every generated operation', () => {
    expect(() => validateCatalog()).not.toThrow()
    expect(workflowCatalog.skills.map((skill) => skill.id)).toEqual(
      Array.from({ length: 20 }, (_, index) => `B${String(index + 1).padStart(2, '0')}`),
    )
    expect(workflowCatalog.skills.flatMap((skill) => skill.operations)).toHaveLength(workflowCatalog.operation_count)
    expect(workflowCatalog.unmapped_operations).toEqual([])
  })

  it('encodes path values and required query parameters without path injection', () => {
    const operation = workflowCatalog.skills
      .flatMap((skill) => skill.operations)
      .find((candidate) => candidate.path === '/v1/timeseries/query')
    expect(operation).toBeDefined()
    const parameters = initialParameters(operation!, { releaseId: 'release-one' })
    parameters.metric = 'facility power/kw'
    expect(buildOperationPath(operation!, parameters)).toContain('metric=facility+power%2Fkw')
    parameters.start = ''
    expect(() => buildOperationPath(operation!, parameters)).toThrow('start is required')
  })

  it('materializes only known release tokens and validates request bodies', () => {
    expect(materializeTokens({ release: '$release_id', source: '$source_revision', value: '$unknown' }, {
      releaseId: 'release-one', sourceRevision: 'abc123def456',
    })).toEqual({ release: 'release-one', source: 'abc123def456', value: '$unknown' })
    const operation = workflowCatalog.skills
      .flatMap((skill) => skill.operations)
      .find((candidate) => candidate.path.endsWith('/run') && candidate.path.includes('/certification/'))
    expect(operationBody(operation!, '{"expected_source_revision":"abc123def456"}')).toBe(
      '{"expected_source_revision":"abc123def456"}',
    )
    expect(() => operationBody(operation!, 'null')).toThrow('request body')
  })

  it('requires confirmation metadata for every high-risk workflow', () => {
    const highRisk = workflowCatalog.skills.flatMap((skill) => skill.operations).filter((operation) => operation.risk === 'high')
    expect(highRisk.length).toBeGreaterThan(0)
    expect(highRisk.every((operation) => operation.requires_confirmation)).toBe(true)
  })
})
