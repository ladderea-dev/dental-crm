import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.AIRZEN_TEST_URL || 'http://localhost:8000';
const parsed = new URL(baseURL);
const safeHosts = new Set(['localhost', '127.0.0.1', '::1']);
if (!safeHosts.has(parsed.hostname) && process.env.ALLOW_STAGING_E2E !== 'true') {
  throw new Error('Refusing non-local E2E target. Set ALLOW_STAGING_E2E=true only for an approved staging environment.');
}

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  reporter: [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]],
  outputDir: 'test-results/playwright',
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
