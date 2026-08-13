import { defineConfig, devices } from '@playwright/test'

const repositoryRoot = '../..'

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  timeout: 45_000,
  reporter: [['list'], ['junit', { outputFile: '../../evidence/web-e2e-results.xml' }]],
  use: {
    baseURL: 'http://127.0.0.1:18881',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'desktop-chrome',
      use: {
        ...devices['Desktop Chrome'],
        ...(process.env.CI ? {} : { channel: 'chrome' }),
      },
    },
  ],
  webServer: [
    {
      command: '.venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 18880',
      cwd: repositoryRoot,
      url: 'http://127.0.0.1:18880/health/ready',
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        COMPUTEWEAVER_ENV: 'simulator',
        COMPUTEWEAVER_DATABASE_URL: 'memory://',
        COMPUTEWEAVER_AUTH_MODE: 'trusted_headers',
      },
    },
    {
      command: '.venv/bin/python -m apps.web.main',
      cwd: repositoryRoot,
      url: 'http://127.0.0.1:18881/web-health/live',
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        COMPUTEWEAVER_ENV: 'simulator',
        COMPUTEWEAVER_WEB_HOST: '127.0.0.1',
        COMPUTEWEAVER_WEB_PORT: '18881',
        COMPUTEWEAVER_WEB_STATIC_ROOT: 'apps/web/dist',
        COMPUTEWEAVER_WEB_API_UPSTREAM: 'http://127.0.0.1:18880',
        COMPUTEWEAVER_WEB_DEV_TENANT: 'tenant-browser',
        COMPUTEWEAVER_WEB_DEV_ACTOR: 'browser-operator',
        COMPUTEWEAVER_WEB_DEV_ROLES: 'admin,operator,safety_admin',
        COMPUTEWEAVER_RELEASE_ID: 'local-candidate',
        COMPUTEWEAVER_RELEASE_COMMIT: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      },
    },
  ],
})
