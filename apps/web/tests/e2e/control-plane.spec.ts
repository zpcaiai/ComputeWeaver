import AxeBuilder from '@axe-core/playwright'
import { expect, test } from '@playwright/test'

test('all twenty skill workspaces are discoverable and contract-bound', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('20 skills')).toBeVisible()
  await expect(page.getByText('120 API operations')).toBeVisible()
  await expect(page.getByText('0 unmapped')).toBeVisible()
  const navigation = page.getByRole('navigation', { name: 'B01 through B20 skill workspaces' })
  await expect(navigation.getByRole('button')).toHaveCount(20)
  for (const id of ['B01', 'B02', 'B03', 'B09', 'B13', 'B16', 'B20']) {
    await navigation.getByRole('button', { name: new RegExp(`^${id}`) }).click()
    await expect(page.getByText(`${id} · governed workspace`)).toBeVisible()
  }
  await navigation.getByRole('button', { name: /^B02/ }).click()
  await expect(page.getByRole('heading', { name: 'Contract registry' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Download OpenAPI' })).toHaveAttribute('href', '/openapi.json')
})

test('operator can execute a deterministic simulation through governed controls', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /^B09/ }).click()
  await expect(page.getByRole('heading', { name: 'Shadow simulator' })).toBeVisible()
  await expect(page.getByLabel('Idempotency key')).not.toHaveValue('')
  await expect(page.getByLabel('Correlation ID')).not.toHaveValue('')
  await page.getByRole('button', { name: 'Submit governed operation' }).click()
  await expect(page.getByText('Operation completed')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Session operation history' })).toBeVisible()
  await expect(page.locator('.run-status.pass')).toContainText('PASS')
})

test('production release surface is fail-closed and has no serious accessibility violations', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /^B20/ }).click()
  await expect(page.locator('#certification-title')).toBeVisible()
  await expect(page.getByText('NOT_CERTIFIED').first()).toBeVisible()
  await expect(page.getByRole('heading', { name: 'External readiness' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Lifecycle event chain' })).toBeVisible()
  await page.getByRole('combobox', { name: /Operation/ }).selectOption({ label: 'POST · Publish Certification' })
  await expect(page.getByLabel('Validated JSON payload')).toHaveValue(/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/)
  await expect(page.getByLabel('Validated JSON payload')).not.toHaveValue(/REPLACE_WITH_IMMUTABLE_SOURCE_REVISION/)
  await page.getByRole('button', { name: 'Submit governed operation' }).click()
  await expect(page.locator('.operation-console [role="alert"]')).toContainText('explicit confirmation')
  const results = await new AxeBuilder({ page }).analyze()
  expect(results.violations.filter((violation) => ['critical', 'serious'].includes(violation.impact || ''))).toEqual([])
})

test('skill workflows remain operable on a narrow operator viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  const navigation = page.getByRole('navigation', { name: 'B01 through B20 skill workspaces' })
  await navigation.getByRole('button', { name: /^B16/ }).click()
  await expect(page.getByRole('heading', { name: 'Guarded execution' })).toBeVisible()
  await expect(page.getByRole('combobox', { name: /Operation/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /operation$/ })).toBeVisible()
})
